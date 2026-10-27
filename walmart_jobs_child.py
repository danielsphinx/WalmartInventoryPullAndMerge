"""One Walmart job step, run in its own process by walmart_jobs.py.

    python walmart_jobs_child.py --results FILE [--dry-run] script PATH [ENTRY [ARG]]
    python walmart_jobs_child.py --results FILE [--dry-run] orders
    python walmart_jobs_child.py --results FILE [--dry-run] tracking --ledger FILE

Every mode wraps requests.post, requests.get and requests.request in this process before any script loads,
so every HTTP call any script makes is recorded as one JSON line in FILE: method, URL without its query
except feedType, HTTP status, the feedId of a feed reply, the request size. Headers and bodies are never
recorded (they carry the credentials and the token). A call with no timeout gets one: (10, 60) for a token,
(10, 300) for anything else.

--dry-run: a POST to anything but /v3/token is not sent. It is recorded with "dry": true and answered with a
fake 202 whose body is {"feedId": "DRYRUN-<n>"}. Token requests and GETs still run: they change nothing.

script: loads PATH by file path (the way UiPath's Load Python Script does), which runs its module level code
(Lag Time Bulk posts at load), then calls ENTRY(ARG) when ENTRY is given. The gate and the feed HTTP helper
load themselves by absolute path, so the working directory does not matter.

orders: the Walmart pending order export, replacing a run of Get Orders.py. The functions of Get Orders.py
are loaded from its source without running its module level calls, so the two CSVs keep the exact columns
and rows Get Orders.py writes. What changes: the orders are fetched once (Get Orders.py fetched twice, so its
two files could disagree); a missing token or any reply other than 200 raises instead of writing an empty or
partial file; each file is written to a temporary name beside it and then moved over the old one, retried
while another process (the robot's Read CSV) holds it. A failed run leaves the previous files untouched.

tracking: the Walmart tracking upload, replacing a run of Order Tracking.py. The payload is built by Order
Tracking.py's own update_order_shipping, called one row at a time. Rows without a real purchaseOrderId
(blank, or a sheet placeholder such as #N/A), lineNumber or trackingNumber are skipped. A row the ledger
already holds with a 2xx status, or with an unknown outcome, is skipped. Every attempt is appended to the
ledger (not in a dry run). Exit 1 when any row failed.

The last line of FILE is {"event": "end", "ok": ..., "error": ...}. Exit 0 only when ok.
"""
from __future__ import annotations

import argparse
import ast
import csv
import datetime as _dt
import importlib.util
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

HERE = Path(__file__).resolve().parent
HTTP_HELPER = HERE / "walmart_feed_http.py"
GET_ORDERS = HERE / "Get Orders.py"
ORDER_TRACKING = HERE / "Order Tracking.py"
TOKEN_PATH = "/v3/token"
DEFAULT_TIMEOUTS = {"token": (10, 60), "other": (10, 300)}
REPLACE_RETRY_SECONDS = 60
ORDERS_OUTPUT_DIR: Path | None = None  # tests only: write the order CSVs here instead of where Get Orders.py names
TRACKING_CSV: Path | None = None  # tests only: read this tracking file instead of Order Tracking.py's
LEDGER_FIELDS = ["time", "purchaseOrderId", "lineNumber", "trackingNumber", "status", "detail"]
_SECRET_RUN = re.compile(r"[A-Za-z0-9_\-]{32,}")
_FEED_ID = re.compile(r"""feedId["'>:\s]+([A-Za-z0-9@_.\-]+)""")


def redact(text: str) -> str:
    return _SECRET_RUN.sub("<redacted>", str(text))


class FakeResponse:
    """The reply a dry run gives to a POST it did not send."""

    def __init__(self, feed_id: str):
        self.status_code = 202
        self.text = json.dumps({"feedId": feed_id})
        self.content = self.text.encode()
        self.headers = {"Content-Type": "application/json"}
        self.ok = True

    def json(self):
        return json.loads(self.text)


class Recorder:
    """Wraps the requests module's call functions in this process and records every call."""

    def __init__(self, results_path: Path, dry_run: bool):
        self.path = results_path
        self.dry_run = dry_run
        self.dry_count = 0
        self.last = None
        self._orig = {"post": requests.post, "get": requests.get, "request": requests.request}

    def install(self):
        requests.post = lambda url, *a, **kw: self.call("POST", url, self._orig["post"], a, kw)
        requests.get = lambda url, *a, **kw: self.call("GET", url, self._orig["get"], a, kw)
        requests.request = lambda method, url, *a, **kw: self.call(
            str(method).upper(), url, lambda u, *aa, **kk: self._orig["request"](method, u, *aa, **kk), a, kw)

    def write(self, row: dict):
        row = {"t": _dt.datetime.now().isoformat(timespec="seconds"), **row}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    @staticmethod
    def short_url(url: str) -> str:
        parts = urlsplit(str(url))
        feed_type = parse_qs(parts.query).get("feedType", [""])[0]
        return parts.path + (f"?feedType={feed_type}" if feed_type else "")

    @staticmethod
    def size(kw: dict) -> int:
        body = kw.get("data") if kw.get("data") is not None else kw.get("json")
        if body is None:
            return 0
        if isinstance(body, (bytes, str)):
            return len(body)
        return len(json.dumps(body, default=str))

    def call(self, method, url, real, args, kw):
        is_token = TOKEN_PATH in str(url)
        row = {"event": "http", "method": method, "url": self.short_url(url), "token": is_token, "bytes": self.size(kw)}
        if method == "POST" and not is_token and self.dry_run:
            self.dry_count += 1
            response = FakeResponse(f"DRYRUN-{self.dry_count}")
            self.write({**row, "dry": True, "status": 202, "feedId": None})
            self.last = response
            return response
        kw.setdefault("timeout", DEFAULT_TIMEOUTS["token" if is_token else "other"])
        try:
            response = real(url, *args, **kw)
        except Exception as exc:
            self.write({**row, "dry": False, "status": None, "error": f"{type(exc).__name__}: {redact(exc)}"[:300]})
            raise
        feed_id = None
        if method == "POST" and not is_token:
            match = _FEED_ID.search(str(getattr(response, "text", "") or "")[:2000])
            feed_id = match.group(1) if match else None
        self.write({**row, "dry": False, "status": getattr(response, "status_code", None), "feedId": feed_id})
        self.last = response
        return response


def load_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def functions_of(path: Path) -> dict:
    """Run a script's imports, assignments and definitions only: never its module level calls, loops or with
    blocks (Get Orders.py writes both files and Order Tracking.py posts every row at module level)."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    keep = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)
    tree.body = [node for node in tree.body if isinstance(node, keep)]
    namespace = {"__name__": f"walmart_jobs_child.{path.stem.replace(' ', '_')}", "__file__": str(path)}
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace


def strict_token() -> str:
    """A token through the feed HTTP helper (timeout, retry on 429, any spelling, refusal by status)."""
    http = load_by_path("walmart_feed_http_child", HTTP_HELPER)
    headers = {
        "Authorization": os.getenv("Walmart_Authorization"),
        "Content-Type": "application/x-www-form-urlencoded",
        "WM_QOS.CORRELATION_ID": os.getenv("WM_QOS_CORRELATION_ID"),
        "WM_SVC.NAME": "My Walmart Inventory",
    }
    response = http.post("https://marketplace.walmartapis.com/v3/token", kind="token", headers=headers,
                         data="grant_type=client_credentials")
    return http.token_from(response)


def require_credentials():
    missing = [n for n in ("Walmart_Authorization", "WM_QOS_CORRELATION_ID") if not os.getenv(n)]
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")


def replace_with_retry(temp: Path, target: Path):
    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(temp, target)
            return
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


# ---------------------------------------------------------------------------------------------- modes

def run_script(path: Path, entry: str | None, arg: str | None):
    module = load_by_path(f"walmart_job_{path.stem.replace(' ', '_')}", path)
    if entry:
        getattr(module, entry)(arg) if arg is not None else getattr(module, entry)()


class StrictRequests:
    """The requests module as Get Orders.py's functions see it: a reply other than 200 raises."""

    def __init__(self):
        self.post, self.request = requests.post, requests.request

    @staticmethod
    def get(url, *args, **kwargs):
        response = requests.get(url, *args, **kwargs)
        if response.status_code != 200:
            raise RuntimeError(f"orders GET {Recorder.short_url(url)} answered HTTP {response.status_code}: "
                               f"{redact(str(response.text)[:200])!r}")
        return response

    def __getattr__(self, name):
        return getattr(requests, name)


def run_orders(dry_run: bool):
    require_credentials()
    ns = functions_of(GET_ORDERS)
    ns["requests"] = StrictRequests()
    ns["get_token"] = strict_token

    original_get_orders, cache = ns["get_orders"], {}

    def get_orders_once(*args, **kwargs):
        if "orders" not in cache:
            cache["orders"] = original_get_orders(*args, **kwargs)
        return cache["orders"]

    ns["get_orders"] = get_orders_once
    targets = {}

    def redirected_open(file, mode="r", *args, **kwargs):
        target = Path(file)
        if "w" in mode and target.suffix.lower() == ".csv":
            if ORDERS_OUTPUT_DIR is not None:
                target = ORDERS_OUTPUT_DIR / target.name
            temp = target.with_name(f"~${target.stem}.tmp")
            targets[temp] = target
            return open(temp, mode, *args, **kwargs)
        return open(file, mode, *args, **kwargs)

    ns["open"] = redirected_open
    try:
        ns["get_json_header_csv"]()
        ns["map_to_second_format"]()
    except BaseException:
        for temp in targets:
            temp.unlink(missing_ok=True)
        raise
    if len(targets) != 2:
        raise RuntimeError(f"orders: expected the two Walmart order CSVs, Get Orders.py wrote {len(targets)}")
    if dry_run:
        for temp, target in targets.items():
            print(f"DRY RUN: would replace {target.name} ({temp.stat().st_size} bytes)")
            temp.unlink(missing_ok=True)
        return
    for temp, target in targets.items():
        replace_with_retry(temp, target)
    print(f"orders: {len(cache.get('orders') or [])} orders written to {len(targets)} files")


def tracking_csv_path() -> Path:
    if TRACKING_CSV is not None:
        return TRACKING_CSV
    tree = ast.parse(ORDER_TRACKING.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "csv_file_path" for t in node.targets):
            return Path(ast.literal_eval(node.value))
    raise RuntimeError("tracking: Order Tracking.py names no csv_file_path")


def real_row(row: dict) -> bool:
    po = (row.get("purchaseOrderId") or "").strip()
    return bool(po) and not po.startswith("#") and bool((row.get("lineNumber") or "").strip()) \
        and bool((row.get("trackingNumber") or "").strip())


def ledger_keys(ledger: Path) -> set:
    """Keys already posted (a 2xx status) or sent with an unknown outcome: neither is posted again."""
    keys = set()
    if not ledger.exists():
        return keys
    with ledger.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            status = row.get("status", "")
            if status.startswith("2") or status in ("unknown", "seeded"):
                keys.add((row["purchaseOrderId"], row["lineNumber"], row["trackingNumber"]))
    return keys


def append_ledger(ledger: Path, row: dict):
    new = not ledger.exists()
    with ledger.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def read_tracking_rows(path: Path) -> list[dict]:
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return [{k.lstrip("﻿"): (v or "") for k, v in row.items() if k is not None} for row in csv.DictReader(f)]


def run_tracking(recorder: Recorder, ledger: Path, dry_run: bool, seed: bool = False):
    rows = read_tracking_rows(tracking_csv_path())
    eligible = [r for r in rows if real_row(r)]
    done = ledger_keys(ledger)
    todo = [r for r in eligible if (r["purchaseOrderId"], r["lineNumber"], r["trackingNumber"]) not in done]
    print(f"tracking: {len(rows)} rows, {len(rows) - len(eligible)} placeholder or incomplete, "
          f"{len(eligible) - len(todo)} already in the ledger, {len(todo)} to post")
    if seed:
        for r in todo:
            append_ledger(ledger, {"time": _dt.datetime.now().isoformat(timespec="seconds"),
                                   "purchaseOrderId": r["purchaseOrderId"], "lineNumber": r["lineNumber"],
                                   "trackingNumber": r["trackingNumber"], "status": "seeded", "detail": "seed"})
        return
    if not todo:
        return
    require_credentials()
    ns = functions_of(ORDER_TRACKING)
    token = {"value": None, "at": 0.0}

    def cached_token():
        if not token["value"] or time.monotonic() - token["at"] > 600:
            token["value"], token["at"] = strict_token(), time.monotonic()
        return token["value"]

    ns["gettoken"] = cached_token
    failed = 0
    for r in todo:
        key = {"purchaseOrderId": r["purchaseOrderId"], "lineNumber": r["lineNumber"], "trackingNumber": r["trackingNumber"]}
        recorder.last = None
        try:
            ns["update_order_shipping"]([r])
            status = str(getattr(recorder.last, "status_code", "none"))
            detail = "" if status.startswith("2") else redact(str(getattr(recorder.last, "text", ""))[:200])
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
            status, detail = "unknown", f"{type(exc).__name__}: sent or not, not retried"
        except Exception as exc:
            status, detail = "error", f"{type(exc).__name__}: {redact(exc)}"[:200]
        if not status.startswith("2"):
            failed += 1
        if not dry_run:
            append_ledger(ledger, {"time": _dt.datetime.now().isoformat(timespec="seconds"), **key,
                                   "status": status, "detail": detail})
        print(f"tracking: {r['purchaseOrderId']} line {r['lineNumber']}: {status}")
    if failed:
        raise RuntimeError(f"tracking: {failed} of {len(todo)} rows failed")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("script")
    p.add_argument("path", type=Path)
    p.add_argument("entry", nargs="?")
    p.add_argument("arg", nargs="?")
    sub.add_parser("orders")
    t = sub.add_parser("tracking")
    t.add_argument("--ledger", required=True, type=Path)
    t.add_argument("--seed", action="store_true")
    args = parser.parse_args(argv)

    recorder = Recorder(args.results, args.dry_run)
    recorder.install()
    ok, error = True, None
    try:
        if args.mode == "script":
            run_script(args.path, args.entry, args.arg)
        elif args.mode == "orders":
            run_orders(args.dry_run)
        else:
            run_tracking(recorder, args.ledger, args.dry_run, seed=args.seed)
    except BaseException as exc:  # SystemExit from a script counts too
        ok = False
        error = f"{type(exc).__name__}: {redact(' '.join(str(exc).split()))}"[:800]
        traceback.print_exc()
    recorder.write({"event": "end", "ok": ok, "error": error, "dry": args.dry_run})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
