from __future__ import annotations

import csv
import io
import os
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
from requests import Response, Session
from requests.exceptions import RequestException, Timeout
# Never relist gate: the gate module beside this file, loaded by its path, holds the one rule.
import importlib.util  # never-relist-gate
_gate_spec = importlib.util.spec_from_file_location("walmart_retired_filter", r"G:\Python Projects\WalmartInventoryPullAndMerge\walmart_retired_filter.py")  # never-relist-gate
_gate = importlib.util.module_from_spec(_gate_spec)  # never-relist-gate
_gate_spec.loader.exec_module(_gate)  # never-relist-gate


TOKEN_URL = "https://marketplace.walmartapis.com/v3/token"
INVENTORY_URL = "https://marketplace.walmartapis.com/v3/inventories"
ITEMS_URL = "https://marketplace.walmartapis.com/v3/items"
REPORT_REQUESTS_URL = "https://marketplace.walmartapis.com/v3/reports/reportRequests"
SERVICE_NAME = "My Walmart Inventory"

OUTPUT_DIR = Path(
    r"G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports\API Python Generated Merged Exports"
)
MERGED_OUTPUT_PATH = OUTPUT_DIR / "Merged Price And Quantity Walmart Export.csv"
QTY_OUTPUT_PATH = OUTPUT_DIR / "Walmart Quantity Export.csv"
PRICES_OUTPUT_PATH = OUTPUT_DIR / "Walmart Prices Export.csv"
INVENTORY_REPORT_REQUEST_ID_PATH = OUTPUT_DIR / "Walmart Inventory Report Request ID.txt"
ITEM_REPORT_REQUEST_ID_PATH = OUTPUT_DIR / "Walmart Item Report Request ID.txt"
RETIRED_SKUS_DIR = Path(os.getenv(
    "WALMART_RETIRED_SKUS_DIR",
    r"C:\Users\Daniel\Downloads\AI\GPT Projects\Walmart Account Quality\walmart-full-checks-and-upgrades\handoff",
))
RETIRED_SKU_FILES = ("retire_approved.csv", "retire_flagged_offensive_approved.csv")

# Intentional safety net: if a SKU existed in the previous merged output but is
# missing from this run, include it with this signal quantity so Walmart updates it.
FALLBACK_QTY = 10
REQUEST_TIMEOUT_SECONDS = 45
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 10
INVENTORY_PAGE_LIMIT = 50
ITEMS_PAGE_LIMIT = 200
MAX_FALLBACK_SKUS_TO_LOG = 100
MIN_INVENTORY_COMPLETENESS_RATIO = 0.90
REPORT_POLL_SECONDS = 300
REPORT_RATE_LIMIT_SLEEP_SECONDS = 300
REPORT_TIMEOUT_SECONDS = 7200


class NonRetryableWalmartError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimitedWalmartError(RuntimeError):
    pass


@dataclass(frozen=True)
class WalmartConfig:
    authorization: str
    correlation_id: str

    @classmethod
    def from_environment(cls) -> "WalmartConfig":
        authorization = os.getenv("Walmart_Authorization", "").strip()
        correlation_id = os.getenv("WM_QOS_CORRELATION_ID", "").strip()
        missing = []

        if not authorization:
            missing.append("Walmart_Authorization")
        if not correlation_id:
            missing.append("WM_QOS_CORRELATION_ID")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Missing required environment variable(s): {joined}")

        return cls(authorization=authorization, correlation_id=correlation_id)


class WalmartClient:
    def __init__(self, config: WalmartConfig, session: Session | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def get_token(self) -> str:
        headers = {
            "Authorization": self.config.authorization,
            "Content-Type": "application/x-www-form-urlencoded",
            "WM_QOS.CORRELATION_ID": self.config.correlation_id,
            "WM_SVC.NAME": SERVICE_NAME,
        }
        response = self._request(
            "POST",
            TOKEN_URL,
            headers=headers,
            data="grant_type=client_credentials",
            retry_label="token request",
        )

        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise RuntimeError(f"Token response was not valid XML: {exc}") from exc

        access_token = find_text(root, "accessToken")
        if not access_token:
            raise RuntimeError("Token response did not include accessToken.")
        return access_token

    def fetch_inventory(self) -> list[list[Any]]:
        if os.getenv("USE_PAGINATED_INVENTORY_API", "").strip() == "1":
            print("[WARNING] USE_PAGINATED_INVENTORY_API=1 is set; using fragile cursor pagination.")
            return self.fetch_inventory_from_paginated_api()
        return self.fetch_inventory_from_report()

    def fetch_inventory_from_report(self) -> list[list[Any]]:
        cached_request_id = load_cached_report_request_id(
            INVENTORY_REPORT_REQUEST_ID_PATH,
            "WALMART_INVENTORY_REPORT_REQUEST_ID",
        )
        report: dict[str, Any] | None = None
        request_id: str | None = None
        if cached_request_id:
            print(f"Resuming inventory report request: {cached_request_id}")
            cached_rows = self.try_download_cached_report(cached_request_id, parse_inventory_report)
            if cached_rows is not None:
                return cached_rows

            try:
                report = self.wait_for_report(cached_request_id, "inventory")
                request_id = cached_request_id
            except RuntimeError as exc:
                print(
                    f"[WARNING] Cached inventory report request {cached_request_id} is no longer "
                    f"usable; discarding it and requesting a fresh report: {exc}"
                )
                clear_cached_report_request_id(INVENTORY_REPORT_REQUEST_ID_PATH)

        if report is None or request_id is None:
            report, request_id = self.request_report(
                report_type="INVENTORY",
                report_version="v1",
                cache_path=INVENTORY_REPORT_REQUEST_ID_PATH,
                label="inventory",
            )

        download_url = first_present(report, "downloadUrl", "downloadURL", "download_url")
        if not download_url:
            download_url = find_link_href(report, "download")
        if not download_url:
            download_url = f"https://marketplace.walmartapis.com/v3/reports/downloadReport?requestId={quote(request_id, safe='')}"

        report_response = self._download_report(str(download_url))
        rows = parse_inventory_report(report_response.content)
        print(f"Inventory report parsed: {len(rows)} SKUs fetched.")
        save_cached_report_request_id(INVENTORY_REPORT_REQUEST_ID_PATH, str(request_id))
        return rows

    def request_report(
        self,
        report_type: str,
        report_version: str,
        cache_path: Path,
        label: str,
    ) -> tuple[dict[str, Any], str]:
        access_token = self.get_token()
        headers = self._auth_headers(access_token, accept="application/json")
        headers["Content-Type"] = "application/json"

        while True:
            try:
                response = self._request(
                    "POST",
                    f"{REPORT_REQUESTS_URL}?reportType={quote(report_type, safe='')}&reportVersion={quote(report_version, safe='')}",
                    headers=headers,
                    json={},
                    retry_label=f"{label} report request",
                )
                break
            except RateLimitedWalmartError as exc:
                print(
                    f"[WARNING] {label.title()} report creation rate-limited; sleeping "
                    f"{REPORT_RATE_LIMIT_SLEEP_SECONDS} seconds: {exc}"
                )
                time.sleep(REPORT_RATE_LIMIT_SLEEP_SECONDS)

        payload = parse_json_response(response, f"{label} report request")
        request_id = first_present(payload, "requestId", "reportRequestId", "id")
        if not request_id:
            raise RuntimeError(f"{label.title()} report request did not return a request ID: {payload}")

        print(f"{label.title()} report requested: {request_id}")
        save_cached_report_request_id(cache_path, str(request_id))
        report = self.wait_for_report(request_id, label)
        return report, str(request_id)

    def try_download_cached_report(
        self,
        request_id: str,
        parser,
    ) -> list[list[Any]] | None:
        download_url = (
            "https://marketplace.walmartapis.com/v3/reports/downloadReport"
            f"?requestId={quote(request_id, safe='')}"
        )
        try:
            report_response = self._download_report(download_url)
            rows = parser(report_response.content)
        except (RuntimeError, RateLimitedWalmartError, NonRetryableWalmartError) as exc:
            print(f"[WARNING] Cached report is not directly downloadable yet: {exc}")
            return None

        print(f"Cached report parsed: {len(rows)} rows fetched.")
        return rows

    def wait_for_report(self, request_id: str, label: str) -> dict[str, Any]:
        deadline = time.monotonic() + REPORT_TIMEOUT_SECONDS
        access_token = self.get_token()
        headers = self._auth_headers(access_token, accept="application/json")

        while True:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"{label.title()} report {request_id} was not ready after "
                    f"{REPORT_TIMEOUT_SECONDS // 60} minutes."
                )

            try:
                response = self._request(
                    "GET",
                    f"{REPORT_REQUESTS_URL}/{quote(request_id, safe='')}",
                    headers=headers,
                    retry_label="inventory report status",
                )
            except RateLimitedWalmartError as exc:
                print(
                    "[WARNING] Report status rate-limited; sleeping "
                    f"{REPORT_RATE_LIMIT_SLEEP_SECONDS} seconds: {exc}"
                )
                time.sleep(REPORT_RATE_LIMIT_SLEEP_SECONDS)
                continue
            except NonRetryableWalmartError as exc:
                if exc.status_code == 401:
                    print("[WARNING] Report status token expired; refreshing token.")
                    access_token = self.get_token()
                    headers = self._auth_headers(access_token, accept="application/json")
                    continue
                raise

            payload = parse_json_response(response, "inventory report status")
            status = str(first_present(payload, "requestStatus", "status") or "").upper()
            print(f"{label.title()} report status: {status or 'UNKNOWN'}")

            if status == "READY":
                return payload
            if status in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
                raise RuntimeError(f"{label.title()} report {request_id} failed: {payload}")

            time.sleep(REPORT_POLL_SECONDS)

    def _download_report(self, download_url: str) -> Response:
        download_url = absolute_walmart_url(download_url)
        access_token = self.get_token()
        auth_headers = {
            "WM_SEC.ACCESS_TOKEN": access_token,
            "WM_QOS.CORRELATION_ID": self.config.correlation_id,
            "WM_SVC.NAME": SERVICE_NAME,
        }

        last_error: Exception | None = None
        for accept in ("application/json", "text/csv", "application/zip", "*/*"):
            headers = dict(auth_headers)
            headers["Accept"] = accept
            try:
                response = self._request(
                    "GET",
                    download_url,
                    headers=headers,
                    retry_label=f"inventory report download ({accept})",
                )
                return self._resolve_report_download_response(response)
            except NonRetryableWalmartError as exc:
                last_error = exc
                if exc.status_code not in {401, 406}:
                    raise

        if last_error:
            print(f"[WARNING] Authenticated report download failed; trying raw URL: {last_error}")

        response = self._request(
            "GET",
            download_url,
            retry_label="inventory report download without auth",
        )
        return self._resolve_report_download_response(response)

    def _resolve_report_download_response(self, response: Response) -> Response:
        content_type = response.headers.get("Content-Type", "")
        if "json" not in content_type.lower() and not response.content.lstrip().startswith(b"{"):
            return response

        payload = parse_json_response(response, "inventory report download")
        signed_url = first_present(payload, "downloadUrl", "downloadURL", "download_url")
        if not signed_url:
            return response

        signed_url = absolute_walmart_url(str(signed_url))
        print("Inventory report signed download URL received.")
        try:
            return self._request(
                "GET",
                signed_url,
                retry_label="inventory report signed download",
            )
        except NonRetryableWalmartError as exc:
            if exc.status_code != 401:
                raise

        access_token = self.get_token()
        headers = self._auth_headers(access_token, accept="*/*")
        return self._request(
            "GET",
            signed_url,
            headers=headers,
            retry_label="inventory report signed download with auth",
        )

    def fetch_inventory_from_paginated_api(self) -> list[list[Any]]:
        access_token = self.get_token()
        headers = self._auth_headers(access_token, accept="application/json")
        next_cursor: str | None = None
        rows: list[list[Any]] = []
        page_number = 0

        while True:
            url = build_inventory_url(next_cursor)

            for auth_attempt in range(2):
                try:
                    response = self._request(
                        "GET",
                        url,
                        headers=headers,
                        retry_label="inventory page",
                    )
                    break
                except NonRetryableWalmartError as exc:
                    if exc.status_code == 401 and auth_attempt == 0:
                        print("[WARNING] Inventory token expired; refreshing token and retrying page.")
                        access_token = self.get_token()
                        headers = self._auth_headers(access_token, accept="application/json")
                        continue
                    if rows:
                        print(f"[WARNING] Inventory pagination stopped early: {exc}")
                        print("[WARNING] Fallback logic will cover SKUs missing from this partial scrape.")
                        return rows
                    raise
            payload = parse_json_response(response, "inventory page")

            if "error" in payload:
                raise RuntimeError(f"Walmart returned an inventory error: {payload}")

            inventories = payload.get("elements", {}).get("inventories")
            if not isinstance(inventories, list):
                raise RuntimeError(f"Unexpected inventory response format: {payload}")

            page_number += 1
            rows.extend(inventory_to_row(item) for item in inventories)
            print(f"Inventory page {page_number}: {len(inventories)} SKUs fetched ({len(rows)} total).")

            next_cursor = payload.get("meta", {}).get("nextCursor")
            if not next_cursor:
                return rows

    def fetch_prices(self) -> list[list[str]]:
        if os.getenv("USE_PAGINATED_PRICING_API", "").strip() == "1":
            print("[WARNING] USE_PAGINATED_PRICING_API=1 is set; using cursor pricing API.")
            return self.fetch_prices_from_paginated_api()
        return self.fetch_prices_from_report()

    def fetch_prices_from_report(self) -> list[list[str]]:
        cached_request_id = load_cached_report_request_id(
            ITEM_REPORT_REQUEST_ID_PATH,
            "WALMART_ITEM_REPORT_REQUEST_ID",
        )
        report: dict[str, Any] | None = None
        request_id: str | None = None
        if cached_request_id:
            print(f"Resuming item report request: {cached_request_id}")
            cached_rows = self.try_download_cached_report(cached_request_id, parse_item_price_report)
            if cached_rows is not None:
                return cached_rows

            try:
                report = self.wait_for_report(cached_request_id, "item")
                request_id = cached_request_id
            except RuntimeError as exc:
                print(
                    f"[WARNING] Cached item report request {cached_request_id} is no longer "
                    f"usable; discarding it and requesting a fresh report: {exc}"
                )
                clear_cached_report_request_id(ITEM_REPORT_REQUEST_ID_PATH)

        if report is None or request_id is None:
            report, request_id = self.request_report(
                report_type="ITEM",
                report_version="v4",
                cache_path=ITEM_REPORT_REQUEST_ID_PATH,
                label="item",
            )

        download_url = first_present(report, "downloadUrl", "downloadURL", "download_url")
        if not download_url:
            download_url = find_link_href(report, "download")
        if not download_url:
            download_url = f"https://marketplace.walmartapis.com/v3/reports/downloadReport?requestId={quote(request_id, safe='')}"

        report_response = self._download_report(str(download_url))
        rows = parse_item_price_report(report_response.content)
        print(f"Item report parsed: {len(rows)} prices fetched.")
        save_cached_report_request_id(ITEM_REPORT_REQUEST_ID_PATH, str(request_id))
        return rows

    def fetch_prices_from_paginated_api(self) -> list[list[str]]:
        access_token = self.get_token()
        headers = self._auth_headers(access_token, accept="application/xml")
        next_cursor = "*"
        rows: list[list[str]] = []
        page_number = 0

        while True:
            url = build_items_url(next_cursor)
            for auth_attempt in range(2):
                try:
                    response = self._request(
                        "GET",
                        url,
                        headers=headers,
                        retry_label="pricing page",
                    )
                    break
                except NonRetryableWalmartError as exc:
                    if exc.status_code == 401 and auth_attempt == 0:
                        print("[WARNING] Pricing token expired; refreshing token and retrying page.")
                        access_token = self.get_token()
                        headers = self._auth_headers(access_token, accept="application/xml")
                        continue
                    raise

            try:
                root = ET.fromstring(response.content)
            except ET.ParseError as exc:
                raise RuntimeError(f"Pricing response was not valid XML: {exc}") from exc

            page_rows = extract_price_rows(root)
            rows.extend(page_rows)
            page_number += 1
            print(f"Pricing page {page_number}: {len(page_rows)} prices fetched ({len(rows)} total).")

            next_cursor = find_text(root, "nextCursor")
            if not next_cursor:
                return rows

    def _auth_headers(self, access_token: str, accept: str) -> dict[str, str]:
        return {
            "WM_SEC.ACCESS_TOKEN": access_token,
            "WM_QOS.CORRELATION_ID": self.config.correlation_id,
            "WM_SVC.NAME": SERVICE_NAME,
            "Accept": accept,
        }

    def _request(self, method: str, url: str, retry_label: str, **kwargs: Any) -> Response:
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    **kwargs,
                )
                if response.status_code == 429:
                    raise RateLimitedWalmartError(
                        f"HTTP {response.status_code}: {trim_response_text(response)}"
                    )
                if response.status_code in {500, 502, 503, 504}:
                    raise RuntimeError(
                        f"HTTP {response.status_code}: {trim_response_text(response)}"
                    )
                if response.status_code >= 400:
                    raise NonRetryableWalmartError(
                        response.status_code,
                        f"HTTP {response.status_code}: {trim_response_text(response)}"
                    )
                response.raise_for_status()
                return response
            except NonRetryableWalmartError:
                raise
            except RateLimitedWalmartError:
                raise
            except (RequestException, Timeout, RuntimeError) as exc:
                last_error = exc
                if attempt == MAX_RETRIES:
                    break
                print(f"Retrying {retry_label} after failure ({attempt}/{MAX_RETRIES}): {exc}")
                time.sleep(RETRY_DELAY_SECONDS)

        raise RuntimeError(f"Failed {retry_label} after {MAX_RETRIES} attempts: {last_error}")


def inventory_to_row(inventory_item: dict[str, Any]) -> list[Any]:
    sku = str(inventory_item.get("sku", "")).strip()
    quantity = 0

    for node in inventory_item.get("nodes", []) or []:
        quantity = node.get("availToSellQty", {}).get("amount", quantity)

    return [sku, quantity]


def build_inventory_url(next_cursor: str | None) -> str:
    url = f"{INVENTORY_URL}?limit={INVENTORY_PAGE_LIMIT}"
    if next_cursor:
        url += f"&nextCursor={quote(next_cursor, safe='')}"
    return url


def build_items_url(next_cursor: str) -> str:
    return f"{ITEMS_URL}?limit={ITEMS_PAGE_LIMIT}&nextCursor={quote(next_cursor, safe='')}"


def parse_inventory_report(content: bytes) -> list[list[Any]]:
    csv_bytes = extract_csv_bytes(content)
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("Inventory report CSV has no headers.")

    sku_column = find_column(reader.fieldnames, "SKU")
    quantity_column = find_column(reader.fieldnames, "AvailToSell Quantity", "Avail To Sell Quantity")
    if not sku_column or not quantity_column:
        raise RuntimeError(
            "Inventory report CSV is missing required columns. "
            f"Found columns: {reader.fieldnames}"
        )

    quantities: dict[str, float] = {}
    for row in reader:
        sku = str(row.get(sku_column, "")).strip()
        if not sku:
            continue
        quantity = parse_quantity(row.get(quantity_column, "0"))
        quantities[sku] = quantities.get(sku, 0) + quantity

    return [[sku, format_quantity(quantity)] for sku, quantity in sorted(quantities.items())]


def parse_item_price_report(content: bytes) -> list[list[str]]:
    csv_bytes = extract_csv_bytes(content)
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("Item report CSV has no headers.")

    sku_column = find_column(reader.fieldnames, "SKU")
    price_column = find_column(
        reader.fieldnames,
        "Price",
        "Current Price",
        "Selling Price",
        "Offer Price",
    )
    if not sku_column or not price_column:
        raise RuntimeError(
            "Item report CSV is missing required pricing columns. "
            f"Found columns: {reader.fieldnames}"
        )

    prices: dict[str, str] = {}
    for row in reader:
        sku = str(row.get(sku_column, "")).strip()
        amount = normalize_price(row.get(price_column, ""))
        if sku and amount:
            prices[sku] = amount

    return [[sku, prices[sku]] for sku in sorted(prices)]


def load_cached_report_request_id(path: Path, env_var_name: str) -> str | None:
    env_request_id = os.getenv(env_var_name, "").strip()
    if env_request_id:
        return env_request_id

    if not path.exists():
        return None

    request_id = path.read_text(encoding="utf-8").strip()
    return request_id or None


def save_cached_report_request_id(path: Path, request_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(request_id, encoding="utf-8")


def clear_cached_report_request_id(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"[WARNING] Could not delete stale report request cache {path}: {exc}")


# The gate's own loader: both lists required, a refusal of a missing or defective list, the
# dated waiver only (ALLOW_UNFILTERED_EXPORT=1 with ALLOW_UNFILTERED_EXPORT_UNTIL), the freshness check.
load_retired_skus = _gate.load_retired_skus  # never-relist-gate


def drop_retired(rows: list[list[Any]], retired: set[str], label: str) -> list[list[Any]]:
    kept = [row for row, keep in zip(rows, _gate.keep_mask([row[0] for row in rows], retired)) if keep]  # never-relist-gate
    dropped = len(rows) - len(kept)
    if dropped:
        print(f"[INFO] Excluded {dropped} retired SKU(s) from the {label} data.")
    return kept


def normalize_price(value: Any) -> str:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    return text


def extract_csv_bytes(content: bytes) -> bytes:
    if zipfile.is_zipfile(io.BytesIO(content)):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not csv_names:
                raise RuntimeError(f"Inventory report ZIP did not contain a CSV: {archive.namelist()}")
            with archive.open(csv_names[0]) as csv_file:
                return csv_file.read()
    return content


def find_column(columns: list[str], *expected_names: str) -> str | None:
    normalized = {normalize_column_name(column): column for column in columns}
    for expected_name in expected_names:
        match = normalized.get(normalize_column_name(expected_name))
        if match:
            return match
    return None


def normalize_column_name(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def parse_quantity(value: Any) -> float:
    text = str(value or "0").strip().replace(",", "")
    if not text:
        return 0
    try:
        return float(text)
    except ValueError:
        return 0


def format_quantity(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return str(value)


def extract_price_rows(root: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []

    for item in iter_elements(root, "ItemResponse"):
        sku = find_text(item, "sku")
        amount = find_text(item, "amount")
        if sku and amount:
            rows.append([sku, amount])

    if rows:
        return rows

    # Fallback for XML shapes that do not wrap each item in ItemResponse.
    current_sku: str | None = None
    for element in root.iter():
        name = local_name(element.tag)
        text = clean_text(element.text)
        if name == "sku" and text:
            current_sku = text
        elif name == "amount" and current_sku and text:
            rows.append([current_sku, text])
            current_sku = None

    return rows


def load_previous_inventory(path: Path) -> dict[str, str]:
    if not path.exists():
        print(f"[WARNING] Previous inventory file not found at: {path}")
        print("[WARNING] Continuing without fallback data.")
        return {}

    try:
        df = pd.read_csv(path, dtype=str).fillna("")
    except Exception as exc:
        print(f"[WARNING] Could not read previous inventory file: {exc}")
        print("[WARNING] Continuing without fallback data.")
        return {}

    required_columns = {"WalmartExportSku", "availToSellQty"}
    if not required_columns.issubset(df.columns):
        print(f"[WARNING] Previous inventory file is missing columns: {sorted(required_columns - set(df.columns))}")
        print("[WARNING] Continuing without fallback data.")
        return {}

    previous_inventory = {
        str(row.WalmartExportSku).strip(): str(row.availToSellQty).strip()
        for row in df.itertuples(index=False)
        if str(row.WalmartExportSku).strip()
    }
    print(f"[INFO] Loaded {len(previous_inventory)} SKUs from previous inventory file.")
    return previous_inventory


def apply_fallback(
    sku_quantity_data: list[list[Any]],
    previous_inventory: dict[str, str],
) -> list[list[Any]]:
    scraped_skus = {str(row[0]).strip() for row in sku_quantity_data if row and row[0]}
    missing_skus = sorted(sku for sku in previous_inventory if sku not in scraped_skus)
    fallback_rows = [[sku, FALLBACK_QTY] for sku in missing_skus]
    final_data = list(sku_quantity_data) + fallback_rows

    print("\n" + "=" * 60)
    print("[SCRAPE SUMMARY]")
    print(f"  SKUs found in this scrape:          {len(scraped_skus)}")
    print(f"  SKUs in previous inventory file:    {len(previous_inventory)}")
    print(f"  SKUs missing from new scrape:       {len(missing_skus)}")
    print(f"  Total SKUs in final output:         {len(final_data)}")

    if missing_skus:
        print(f"\n[FALLBACK SKUs] Added with qty={FALLBACK_QTY} because they were missing from this scrape:")
        for sku in missing_skus[:MAX_FALLBACK_SKUS_TO_LOG]:
            print(f"    {sku}")
        remaining = len(missing_skus) - MAX_FALLBACK_SKUS_TO_LOG
        if remaining > 0:
            print(f"    ... {remaining} more fallback SKUs not shown")
    else:
        print("\n[FALLBACK SKUs] None. All previous SKUs were found in this scrape.")

    print("=" * 60 + "\n")
    return final_data


def validate_inventory_completeness(
    sku_quantity_data: list[list[Any]],
    previous_inventory: dict[str, str],
) -> None:
    if not previous_inventory:
        return

    scraped_skus = {str(row[0]).strip() for row in sku_quantity_data if row and row[0]}
    ratio = len(scraped_skus) / len(previous_inventory)

    if ratio >= MIN_INVENTORY_COMPLETENESS_RATIO:
        return

    if os.getenv("ALLOW_PARTIAL_INVENTORY_EXPORT", "").strip() == "1":
        print(
            "[WARNING] Live inventory scrape is far smaller than the previous file "
            f"({len(scraped_skus):,}/{len(previous_inventory):,}, {ratio:.1%}), "
            "but ALLOW_PARTIAL_INVENTORY_EXPORT=1 is set."
        )
        return

    raise RuntimeError(
        "Live inventory scrape is too incomplete to safely write production exports: "
        f"{len(scraped_skus):,} live SKUs vs {len(previous_inventory):,} previous SKUs "
        f"({ratio:.1%}). This usually means Walmart pagination failed. "
        "Set ALLOW_PARTIAL_INVENTORY_EXPORT=1 only if you intentionally want fallback "
        f"quantity {FALLBACK_QTY} applied to the missing SKUs."
    )


def write_inventory_csv(sku_quantity_data: list[list[Any]], path: Path = QTY_OUTPUT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["sku", "availToSellQty"])
        writer.writerows(sku_quantity_data)

    print(f"Inventory CSV written: {path}")


def write_price_csv(sku_amount_data: list[list[str]], path: Path = PRICES_OUTPUT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["SKU", "Amount"])
        writer.writerows(sku_amount_data)

    print(f"Price CSV written: {path}")


def write_merged_csv(
    prices_path: Path = PRICES_OUTPUT_PATH,
    quantity_path: Path = QTY_OUTPUT_PATH,
    output_path: Path = MERGED_OUTPUT_PATH,
) -> None:
    print("Loading Prices CSV...")
    prices = pd.read_csv(prices_path, encoding="utf-8", dtype=str).fillna("")

    print("Loading Qty CSV...")
    quantities = pd.read_csv(quantity_path, encoding="utf-8", dtype=str).fillna("")

    validate_columns(prices, {"SKU", "Amount"}, prices_path)
    validate_columns(quantities, {"sku", "availToSellQty"}, quantity_path)

    prices = prices.drop_duplicates(subset=["SKU"], keep="last")
    quantities = quantities.drop_duplicates(subset=["sku"], keep="last")

    print("Merging DataFrames...")
    merged = pd.merge(quantities, prices, left_on="sku", right_on="SKU", how="left")
    merged = merged.rename(columns={"sku": "WalmartExportSku"})
    merged["Amount"] = merged["Amount"].replace("", "0").fillna("0")
    merged = merged.drop(columns=["SKU"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(output_path, encoding="utf-8", index=False)
    print(f"Merged CSV written: {output_path}")


def validate_columns(df: pd.DataFrame, required_columns: set[str], path: Path) -> None:
    missing = required_columns - set(df.columns)
    if missing:
        raise RuntimeError(f"{path} is missing required column(s): {sorted(missing)}")


def parse_json_response(response: Response, label: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} response was not valid JSON: {trim_response_text(response)}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} response was not a JSON object: {payload}")
    return payload


def first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return value
    return None


def find_link_href(payload: dict[str, Any], rel: str) -> str | None:
    links = payload.get("links")
    if not isinstance(links, list):
        return None

    for link in links:
        if not isinstance(link, dict):
            continue
        if str(link.get("rel", "")).lower() == rel.lower() and link.get("href"):
            return str(link["href"])
    return None


def absolute_walmart_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if not url.startswith("/"):
        url = "/" + url
    return "https://marketplace.walmartapis.com" + url


def trim_response_text(response: Response, limit: int = 500) -> str:
    text = response.text.replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def iter_elements(root: ET.Element, tag_name: str):
    for element in root.iter():
        if local_name(element.tag) == tag_name:
            yield element


def find_text(root: ET.Element, tag_name: str) -> str | None:
    for element in root.iter():
        if local_name(element.tag) == tag_name:
            return clean_text(element.text)
    return None


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def clean_text(text: str | None) -> str | None:
    if text is None:
        return None
    value = text.strip()
    return value or None


def main() -> None:
    config = WalmartConfig.from_environment()
    client = WalmartClient(config)

    retired_skus = load_retired_skus()
    previous_inventory = load_previous_inventory(MERGED_OUTPUT_PATH)
    retired_in_previous = _gate.keep_mask(list(previous_inventory), retired_skus).count(False)  # never-relist-gate
    if retired_in_previous:
        print(f"[INFO] Excluded {retired_in_previous} retired SKU(s) from the previous "
              "inventory baseline (retired SKUs never receive the fallback quantity).")
        previous_inventory = {
            sku: qty for (sku, qty), keep in zip(previous_inventory.items(), _gate.keep_mask(list(previous_inventory), retired_skus)) if keep  # never-relist-gate
        }
    sku_quantity_data = client.fetch_inventory()
    sku_quantity_data = drop_retired(sku_quantity_data, retired_skus, "inventory")
    validate_inventory_completeness(sku_quantity_data, previous_inventory)
    sku_amount_data = client.fetch_prices()
    sku_amount_data = drop_retired(sku_amount_data, retired_skus, "price")
    sku_quantity_data = apply_fallback(sku_quantity_data, previous_inventory)

    write_inventory_csv(sku_quantity_data)
    write_price_csv(sku_amount_data)
    write_merged_csv()


if __name__ == "__main__":
    main()
