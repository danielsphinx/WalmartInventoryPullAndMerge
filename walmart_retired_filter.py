"""Never relist gate for the Walmart upload scripts.

Deployed beside the scripts in G:\\Python Projects\\WalmartInventoryPullAndMerge and loaded by
file path (never through sys.path) by every script there that posts a feed: the five the UiPath
workflow "Shopify Marketplace Converter - DT Filters" loads (New Products Unmatched, New Products By
Match, Update Inventory, Update Pricing, Lag Time Bulk) and the two bundle scripts beside them (New
Unmatched Bundles, New_Unmatched_Bundles_v5), and by main.py, the price and quantity export, whose
load_retired_skus is this module's and whose drop_retired decides with keep_mask. A SKU on the
retire lists, or a row carrying a GTIN on them, never reaches a feed. The patched scripts load the
lists before their token request, so a refusal sends nothing at all.

What is proven: 15 gate SKUs (all AMS-B- bundles) were found in the three latest MP_ITEM item
setup feeds (compliance repo handoff/feed_screen_hits_2026-10-05_212325Z.csv), and retired items
came back ACTIVE on 2026-08-18, 2026-09-24, and 2026-10-05. The compliance record (FRONTIER W-6)
names the UiPath driven scripts as the vector for the 2026-10-05 relistings; that these scripts
rather than another submitter sent those feeds is not shown by a log, only that each of them would
send a retired SKU (the kit's tests run the originals and the patched copies side by side). What is
not proven: that a match, price, inventory, or lag time feed alone re creates a listing; those feeds
were not screened. They are gated too, since dropping a retired SKU from them costs nothing.

The lists. The compliance clone's handoff/retire_approved.csv and
handoff/retire_flagged_offensive_approved.csv, BOTH required. A list file that is missing, zero
bytes, header only (no SKU), without a sku column, with more than one sku column (sku and SKU) or
more than one gtin column, cut off (no terminating line break, or a last row shorter than the
header), or changed while it was being read is read again briefly (the clone is pulled by another
process, which replaces files) and then refused, naming the file. The clone's .git/FETCH_HEAD must
be younger than WALMART_RETIRED_MAX_AGE_HOURS (default 48, 0 = off). A refusal is a RuntimeError
before any feed. Each load prints a per file report (rows, SKUs, blank, duplicate, GTINs). A list
without a gtin column still gates its SKUs; the report and the GTIN log line say it gave no GTIN.

SKU comparison. One canonical key (canonical_sku) on both sides of every comparison: pandas'
int or float coercion of an all digit column (12345, 12345.0), None / NaN / pandas NA (blank),
surrounding whitespace, non breaking spaces, zero width marks and other width variants (NFKC),
letter case (case folded), and leading zeros of an all digit SKU (00123 = 123). Case and leading
zero insensitivity can over match: a different SKU that differs only that way is dropped too.
That is the safe side for a never relist gate, and it is an owner decision (confirm Walmart
treats such SKUs as the same item, or narrow the key).

GTIN comparison. The gtin column of both lists is loaded into a second set (RetiredSkus.gtins) under
its own key (canonical_gtin): the digits only, without leading zeros (GTIN-14 padding varies:
00850013967087 = 850013967087), an integral int or float as its digits; a value with a letter or
any other character, a non integral number, all zeros, or blank is no GTIN. The new product
scripts call drop_retired_gtin_rows right after the SKU gate with the GTIN each row would be sent
with (New Products Unmatched and the bundle scripts send productId, except "CUSTOM" for a bundle
SKU containing '-B-', which carries no GTIN; New Products By Match sends productId): a row whose
GTIN is retired is dropped even when its SKU is new, and a row without a GTIN passes this second
gate. Its log line says "dropped N by GTIN".

Declared exemptions: none. A blank SKU (empty, whitespace, None or NaN, or only invisible marks) is
dropped like a retired one: Walmart rejects such a row, sometimes the whole feed. The log line
counts those rows on their own ("N blank SKU row(s) dropped").

Settings are configuration, not properties of the module, and each is read from the process
environment on every call (UiPath keeps one interpreter, and this module, alive across cycles),
falling back to the built in default; an empty variable is not a setting:
WALMART_RETIRED_SKUS_DIR (lists), WALMART_RETIRED_FILTER_LOG (read back log, default
G:\\Documents And Logs\\walmart_retired_filter.log), WALMART_RETIRED_MAX_AGE_HOURS,
WALMART_RETIRED_RETRY_SECONDS, WALMART_GATE_LOG_MAX_AGE_HOURS, WALMART_GATE_LOG_ROTATE_BYTES, and the
waiver ALLOW_UNFILTERED_EXPORT=1 together with ALLOW_UNFILTERED_EXPORT_UNTIL=YYYY-MM-DD, today or at
most 7 days ahead. Anything else (a bare =1, no date, a bad, past or far date) still refuses. A
waiver sends with whatever lists could be read and every log line says UNFILTERED until the date.
main.py no longer has a waiver of its own: it uses this one.

The read back log. Every call appends ONE line: what it ran on (lists directory and the layer
that chose it, per list SKU counts with the per file report, gate size, newest list mtime, pull
age, UNFILTERED with the waiver end date when waived), rows kept, rows dropped, distinct SKUs
dropped per list, rows in, blank SKU rows dropped, then the dropped SKUs as they appeared in the
feed (the GTIN gate's line: the same for GTINs, and rows without a GTIN). The line is read back
after writing; an unwritable or unconfirmed log warns at most once an hour and never changes what
is filtered. The append rotates the log first when it is over WALMART_GATE_LOG_ROTATE_BYTES (20 MB):
the file is renamed to <name>.<YYYY-MM-DD>.log beside it (<name>.<date>.2.log and so on for a second
rotation that day; a rename never replaces a file), the line goes to a new file, and only the ten
newest rotated files are kept. A rename that fails (another process holds the file) leaves the log
where it is and the line is appended to it: no line is lost. The feed HTTP helper
(walmart_feed_http.py) appends its EXCEPTION lines (a script's entry function raised) through the
same append, _write_log_line. `python walmart_retired_filter.py --status` exits 1 when the lists
cannot be trusted (missing, defective, stale clone) or the log shows no gate run (a filter or
REFUSED line; an EXCEPTION line is not one) in 6 hours (schedule it, or tools/gate_watchdog.py,
which runs it); --status never writes the log itself, so its own refusals cannot make the log look
fresh.

This file is tracked in the patch kit's owner_scripts/; tools/check_live_tree.py reads a deployment
back.
"""

from __future__ import annotations

import csv
import datetime
import glob
import io
import math
import numbers
import os
import re
import sys
import time
import types
import unicodedata
from pathlib import Path
from typing import Iterable

DEFAULT_RETIRED_SKUS_DIR = (
    r"C:\Users\Daniel\Downloads\AI\GPT Projects\Walmart Account Quality\walmart-full-checks-and-upgrades\handoff"
)
MIRROR_RETIRED_SKUS_DIR = r"G:\Python Projects\WalmartInventoryPullAndMerge\retired_lists"  # byte copies of the two lists, kept
# in step by the compliance session after every push; used when the clone path above does not exist
DEFAULT_LOG_PATH = r"G:\Documents And Logs\walmart_retired_filter.log"
RETIRED_SKU_FILES = ("retire_approved.csv", "retire_flagged_offensive_approved.csv")
RETRY_ATTEMPTS = 3
RETRY_SECONDS = 1.0       # WALMART_RETIRED_RETRY_SECONDS overrides it (the tests set 0)
MAX_AGE_HOURS = 48.0      # WALMART_RETIRED_MAX_AGE_HOURS overrides it; 0 switches the age check off
LOG_MAX_AGE_HOURS = 6.0   # the cycle runs about every 2 to 3 hours; WALMART_GATE_LOG_MAX_AGE_HOURS overrides it
WAIVER_MAX_DAYS = 7
DECLARED_EXEMPTIONS = ()  # a blank SKU row is dropped like a retired one (see the module doc)
LOG_ROTATE_BYTES = 20 * 1024 * 1024  # WALMART_GATE_LOG_ROTATE_BYTES overrides it (the tests set a small one)
LOG_ROTATED_KEEP = 10     # rotated logs kept beside the log, the newest by the date in their name


# --------------------------------------------------------------------------------------------------
# settings: read on every call, never at import
# --------------------------------------------------------------------------------------------------

def _setting(name: str) -> str:
    """An environment setting; an empty value is not a setting (Path("") would be the current directory)."""
    return os.getenv(name, "").strip()


def _number(name: str, default: float) -> float:
    try:
        return float(_setting(name) or default)
    except ValueError:
        return default


def _lists_dir() -> tuple[Path, str]:
    """The lists directory and the layer that chose it: the override, else the compliance clone's
    handoff (this machine), else the mirror of the two lists on G: (the robot machine, where the
    clone's path does not exist). When neither directory exists the clone path is named, so the
    refusal says where the lists were expected."""
    override = _setting("WALMART_RETIRED_SKUS_DIR")
    if override:
        return Path(override), "WALMART_RETIRED_SKUS_DIR"
    if Path(DEFAULT_RETIRED_SKUS_DIR).is_dir():
        return Path(DEFAULT_RETIRED_SKUS_DIR), "built in default"
    if Path(MIRROR_RETIRED_SKUS_DIR).is_dir():
        return Path(MIRROR_RETIRED_SKUS_DIR), "built in fallback: the G: mirror, the clone path is absent on this machine"
    return Path(DEFAULT_RETIRED_SKUS_DIR), "built in default"


def retired_skus_dir() -> Path:
    return _lists_dir()[0]


def log_path() -> Path:
    return Path(_setting("WALMART_RETIRED_FILTER_LOG") or DEFAULT_LOG_PATH)


def _today() -> datetime.date:
    return datetime.date.today()


def _waiver_until() -> tuple[datetime.date | None, str]:
    """The end date of the unfiltered waiver, or why the waiver does not hold.

    ALLOW_UNFILTERED_EXPORT=1 alone never waives: it must come with ALLOW_UNFILTERED_EXPORT_UNTIL=YYYY-MM-DD,
    today or later and at most WAIVER_MAX_DAYS days ahead. A waiver that is not removed stops working
    on its own.
    """
    if _setting("ALLOW_UNFILTERED_EXPORT") != "1":
        return None, "ALLOW_UNFILTERED_EXPORT=1 is not set"
    raw = _setting("ALLOW_UNFILTERED_EXPORT_UNTIL")
    if not raw:
        return None, "ALLOW_UNFILTERED_EXPORT_UNTIL=YYYY-MM-DD is not set, so the waiver has no end"
    try:
        until = datetime.date.fromisoformat(raw)
    except ValueError:
        return None, f"ALLOW_UNFILTERED_EXPORT_UNTIL={raw!r} is not a YYYY-MM-DD date"
    today = _today()
    if until < today:
        return None, f"the waiver expired on {until}"
    if (until - today).days > WAIVER_MAX_DAYS:
        return None, f"the waiver end {until} is more than {WAIVER_MAX_DAYS} days away"
    return until, ""


def pull_age_hours(lists_dir: Path) -> float | None:
    """Hours since the clone holding the lists last fetched (.git/FETCH_HEAD is rewritten by every
    fetch and pull, even when nothing changed, so it is the clone's pulse). None when the lists are
    not in a clone we can see, so a copied or overridden directory is not judged on age."""
    try:
        return max(0.0, (time.time() - (Path(lists_dir).parent / ".git" / "FETCH_HEAD").stat().st_mtime) / 3600)
    except OSError:
        return None


# --------------------------------------------------------------------------------------------------
# the SKU key
# --------------------------------------------------------------------------------------------------

_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


def canonical_sku(value: object) -> str:
    """The one form two spellings of the same SKU share. Applied to both sides of every comparison.

    pandas turns an all digit SKU column into int (leading zeros lost) or float (a blank row makes
    12345 into 12345.0); spreadsheets add padding, non breaking spaces and zero width marks; case
    and width variants come from hand edits. None, NaN and pandas NA are blank (""). All digit SKUs
    compare without leading zeros and letters compare case folded: a different SKU that collides
    is dropped too, which is the safe side for a never relist gate (an owner decision).
    """
    if value is None:
        return ""
    if isinstance(value, numbers.Integral):
        text = str(int(value))
    elif isinstance(value, numbers.Real):
        number = float(value)
        if math.isnan(number):
            return ""
        text = str(int(number)) if number.is_integer() else repr(number)
    else:
        text = str(value)
        if text == "<NA>":  # pandas NA, whose truth value raises
            return ""
    text = unicodedata.normalize("NFKC", text).translate(_INVISIBLE).strip().casefold()
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text


_INTEGRAL_TEXT = re.compile(r"([0-9]+)\.0+")
_GTIN_TEXT = re.compile(r"[0-9][0-9 -]*")


def canonical_gtin(value: object) -> str:
    """The one form two spellings of the same GTIN share: its digits without leading zeros (a GTIN-14
    padded with zeros, a 12 or 13 digit form, pandas' int or float of the column). "" for no GTIN:
    None, NaN, blank, all zeros, a non integral number, or text holding anything but digits, spaces
    and hyphens (CUSTOM, a SKU)."""
    if value is None:
        return ""
    if isinstance(value, numbers.Integral):
        text = str(int(value))
    elif isinstance(value, numbers.Real):
        number = float(value)
        if math.isnan(number) or not number.is_integer():
            return ""
        text = str(int(number))
    else:
        text = unicodedata.normalize("NFKC", str(value)).translate(_INVISIBLE).strip()
        integral = _INTEGRAL_TEXT.fullmatch(text)
        if integral:
            text = integral.group(1)
        if not _GTIN_TEXT.fullmatch(text):
            return ""
    return "".join(c for c in text if c.isdigit()).lstrip("0")


# --------------------------------------------------------------------------------------------------
# the lists
# --------------------------------------------------------------------------------------------------

class RetiredSkus(set):
    """The retired SKUs as listed, plus what was read to get them (``report``), which list each
    canonical SKU came from (``sources``), the retired GTINs (``gtins``, canonical) and which list
    each came from (``gtin_sources``)."""

    report: dict = {}
    sources: dict = {}
    gtins: frozenset = frozenset()
    gtin_sources: dict = {}


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


def _parse_list(path: Path) -> tuple[list[str], dict, set[str]]:
    """The SKUs of one list file (as listed), what the read counted, and its GTINs (canonical; none
    when the file has no gtin column). Raises ValueError naming the defect."""
    text = path.read_bytes().decode("utf-8-sig")
    if not text.strip():
        raise ValueError("is empty (zero bytes, no header row)")
    if not text.endswith(("\n", "\r")):
        raise ValueError("looks cut short: it does not end with a line break")
    rows = [row for row in csv.reader(io.StringIO(text, newline="")) if row]
    header = [(h or "").strip().lower() for h in rows[0]]
    sku_columns = [i for i, h in enumerate(header) if h == "sku"]
    if not sku_columns:
        raise ValueError(f"has no sku column (columns: {', '.join(header)})")
    if len(sku_columns) > 1:
        raise ValueError("has more than one sku column; which one counts is not defined")
    gtin_columns = [i for i, h in enumerate(header) if h == "gtin"]
    if len(gtin_columns) > 1:
        raise ValueError("has more than one gtin column; which one counts is not defined")
    body = rows[1:]
    if body and len(body[-1]) < len(header):
        raise ValueError("looks cut short: its last row has fewer columns than the header")
    column = sku_columns[0]
    skus, seen, blank, duplicate, gtins = [], set(), 0, 0, set()
    for row in body:
        if gtin_columns and gtin_columns[0] < len(row) and canonical_gtin(row[gtin_columns[0]]):
            gtins.add(canonical_gtin(row[gtin_columns[0]]))
        sku = row[column].strip() if column < len(row) else ""
        key = canonical_sku(sku)
        if not key:
            blank += 1
            continue
        if key in seen:
            duplicate += 1
        seen.add(key)
        skus.append(sku)
    if not seen:
        raise ValueError(f"holds no SKU ({'header only' if not body else f'{len(body)} rows, all blank'})")
    return skus, {"rows": len(body), "skus": len(seen), "blank": blank, "duplicate": duplicate}, gtins


def _read_list(path: Path) -> tuple[list[str], dict, set[str]]:
    """One list, read until two looks agree: a file that is missing or defective for the moment
    (the clone's puller replaces files), or that changed while it was being read, is read again
    before it is believed. Raises OSError with the last reason once the attempts run out."""
    reason = "not read"
    for attempt in range(RETRY_ATTEMPTS):
        if attempt:
            time.sleep(_number("WALMART_RETIRED_RETRY_SECONDS", RETRY_SECONDS))
        try:
            before = _signature(path)
            parsed = _parse_list(path)
            after = _signature(path)
        except FileNotFoundError:
            reason = "not found"
            continue
        except (OSError, ValueError) as exc:  # locked by the pull, half written, or defective
            reason = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
            continue
        if before != after:
            reason = "it changed while it was being read"
            continue
        return parsed
    raise OSError(reason)


def load_retired_skus() -> RetiredSkus:
    """Every SKU on the retire lists (and, as .gtins, every GTIN), with a report of what was read.
    Refuses (RuntimeError naming each failing file, written to the log as REFUSED) a gate it cannot
    trust, unless the dated waiver holds (see the module doc)."""
    return _load(log_refusal=True)


def _load(log_refusal: bool) -> RetiredSkus:
    directory, layer = _lists_dir()
    retired = RetiredSkus()
    retired.sources, retired.gtin_sources = {}, {}
    gtins: set[str] = set()
    files, gtin_counts, problems = {}, {}, []
    newest = 0.0
    for name in RETIRED_SKU_FILES:
        path = directory / name
        try:
            skus, stats, file_gtins = _read_list(path)
            newest = max(newest, path.stat().st_mtime)
        except OSError as exc:
            problems.append(f"{path}: {exc}")
            continue
        files[name] = stats
        gtin_counts[name] = len(file_gtins)
        for sku in skus:
            retired.add(sku)
            sources = retired.sources.setdefault(canonical_sku(sku), [])
            if name not in sources:
                sources.append(name)
        for gtin in sorted(file_gtins):
            gtins.add(gtin)
            retired.gtin_sources.setdefault(gtin, []).append(name)
    retired.gtins = frozenset(gtins)
    age = pull_age_hours(directory)
    limit = _number("WALMART_RETIRED_MAX_AGE_HOURS", MAX_AGE_HOURS)
    if age is not None and limit > 0 and age > limit:
        problems.append(
            f"the compliance clone at {directory.parent} was last fetched {age:.1f} h ago (limit {limit:g} h); "
            "the process that keeps it pulled may have stopped, so the lists may be missing newer retirements"
        )
    retired.report = {
        "dir": str(directory), "dir_source": layer, "files": files, "gtins": gtin_counts, "problems": problems,
        "unfiltered": False,
        "waiver_until": None, "pulled_hours": age,
        "newest_modified": datetime.datetime.fromtimestamp(newest).isoformat(timespec="seconds") if newest else None,
    }
    pulled = "pull age unknown" if age is None else f"pulled {age:.1f} h ago"
    if problems:
        detail = "; ".join(problems)
        refusal = (f"The never relist gate cannot be built: {detail}. A feed without the whole gate re creates "
                   "policy retired listings, so nothing was sent. Fix the lists (git pull the compliance clone, or "
                   "set WALMART_RETIRED_SKUS_DIR), or waive on purpose with ALLOW_UNFILTERED_EXPORT=1 and "
                   f"ALLOW_UNFILTERED_EXPORT_UNTIL=YYYY-MM-DD (at most {WAIVER_MAX_DAYS} days ahead).")
        until, why_not = _waiver_until()
        if until is None:
            if _setting("ALLOW_UNFILTERED_EXPORT") == "1":
                refusal = f"{refusal} The waiver ALLOW_UNFILTERED_EXPORT=1 does not apply: {why_not}."
            if log_refusal:
                _log_refusal(refusal)
            raise RuntimeError(refusal)
        retired.report.update(unfiltered=True, waiver_until=until.isoformat())
        for problem in problems:
            print(f"[WARNING] Retired SKU list problem: {problem}")
        print(f"[WARNING] Waiver ALLOW_UNFILTERED_EXPORT=1 holds until {until}; feeds go out UNFILTERED with only "
              f"the lists that loaded ({len(retired)} SKUs, {len(retired.gtins)} GTINs, {pulled}).")
        return retired
    for name, stats in files.items():
        print(f"[INFO] {name}: {stats['rows']} rows, {stats['skus']} SKUs, {stats['blank']} blank, "
              f"{stats['duplicate']} duplicate, {gtin_counts[name]} GTINs.")
    print(f"[INFO] Loaded {len(retired)} retired SKUs and {len(retired.gtins)} retired GTINs ({pulled}); "
          "none of them will be sent to Walmart.")
    return retired


# --------------------------------------------------------------------------------------------------
# filtering and the read back log
# --------------------------------------------------------------------------------------------------

def keep_mask(skus: Iterable[object], retired: set[str]) -> list[bool]:
    """True where the SKU may be sent: not blank and not retired. A blank SKU row is dropped (no
    exemption): Walmart rejects it, sometimes with the whole feed. main.py's drop_retired uses this."""
    canonical_retired = {canonical_sku(r) for r in retired}
    canonical_retired.discard("")
    return [bool(key) and key not in canonical_retired for key in map(canonical_sku, skus)]


def gtin_keep_mask(gtins: Iterable[object], retired: set[str]) -> list[bool]:
    """True where the GTIN may be sent: no GTIN at all, or one not on the lists (retired.gtins; a
    plain set of SKUs has none)."""
    retired_gtins = getattr(retired, "gtins", frozenset())
    return [not key or key not in retired_gtins for key in map(canonical_gtin, gtins)]


def _partition(skus: list, retired: set[str]) -> tuple[list[bool], dict]:
    """Which rows may be sent, and the facts the log line states about the others. Counts are per
    grain: rows (what the feed would have carried) and distinct SKUs (what was actually retired)."""
    mask = keep_mask(skus, retired)
    sources = getattr(retired, "sources", {})
    dropped: dict[str, str] = {}
    blank = retired_rows = 0
    for sku, keep in zip(skus, mask):
        key = canonical_sku(sku)
        if not key:
            blank += 1
        elif not keep:
            retired_rows += 1
            dropped.setdefault(key, str(sku).strip())
    by_list: dict[str, int] = {}
    for key in dropped:
        for name in sources.get(key, ["unattributed"]):
            by_list[name] = by_list.get(name, 0) + 1
    return mask, {"rows_in": len(mask), "rows_dropped": retired_rows, "distinct": len(dropped),
                  "names": list(dropped.values()), "by_list": by_list, "blank": blank}


def _ran_on(retired: set[str], kind: str = "SKU") -> str:
    """What the call ran on: the gate size (retired SKUs, or for kind "GTIN" retired GTINs), where
    the lists came from, and whether it was filtered."""
    report = getattr(retired, "report", None)
    size = len(retired) if kind == "SKU" else len(getattr(retired, "gtins", ()))
    gate = "gate" if kind == "SKU" else "GTIN gate"
    if not report:
        text = f"{gate} holds {size} retired {kind}s (a plain set, not loaded from the lists)"
        return text + ("; UNFILTERED: the gate is empty" if kind == "SKU" and not retired else "")
    if kind == "SKU":
        per_list = ", ".join(f"{name}={s['skus']} ({s['rows']} rows, {s['blank']} blank, {s['duplicate']} duplicate)"
                             for name, s in report["files"].items())
    else:
        per_list = ", ".join(f"{name}={count}" for name, count in report.get("gtins", {}).items())
    pulled = "pull age unknown" if report["pulled_hours"] is None else f"pulled {report['pulled_hours']:.1f} h ago"
    text = (f"{gate} holds {size} retired {kind}s on the lists in {report['dir']} ({report['dir_source']}): "
            f"{per_list or 'no list loaded'}; newest modified {report['newest_modified']}, {pulled}")
    if report["unfiltered"]:
        text += f"; UNFILTERED, waiver until {report['waiver_until']}, problems: {'; '.join(report['problems'])}"
    elif not size:
        text += "; UNFILTERED: the gate is empty" if kind == "SKU" else "; no GTIN on the lists: the GTIN gate drops nothing"
    return text


def _warned() -> dict:
    """When each warning was last printed, shared by every copy of this module in the process: each
    patched script loads the gate by file path, so a long lived interpreter holds up to five copies."""
    holder = sys.modules.get("_walmart_retired_filter_warned")
    if holder is None:
        holder = types.ModuleType("_walmart_retired_filter_warned")
        holder.last = {}
        sys.modules[holder.__name__] = holder
    return holder.last


def _warn_every(key: str, text: str, seconds: float = 3600.0) -> None:
    """Print a warning on the first call and then at most once per `seconds`: the gate runs for each
    of about 34 feeds every cycle, and the same line repeated on all of them is read by no one."""
    last = _warned()
    now = time.time()
    if key in last and now - last[key] < seconds:
        return
    last[key] = now
    print(text)


def _rotated_logs(path: Path) -> list[Path]:
    """The rotated copies of the log beside it (<stem>.<YYYY-MM-DD>[.<n>]<suffix>), oldest first."""
    pattern = re.compile(rf"{re.escape(path.stem)}\.(\d{{4}}-\d{{2}}-\d{{2}})(?:\.(\d+))?{re.escape(path.suffix)}")
    found = []
    for candidate in path.parent.glob(f"{glob.escape(path.stem)}.*{glob.escape(path.suffix)}"):
        match = pattern.fullmatch(candidate.name)
        if match:
            found.append(((match.group(1), int(match.group(2) or 1)), candidate))
    return [candidate for _, candidate in sorted(found)]


def _rotate_if_due(path: Path) -> None:
    """Over WALMART_GATE_LOG_ROTATE_BYTES (20 MB), rename the log to <stem>.<today>.log beside it
    (.2, .3 ... for another rotation the same day; a rename never replaces a file) and keep the ten
    newest rotated logs. Called by the append before it writes, so the line goes to the new file. A
    rename that fails (another process holds the log, or rotated it first) changes nothing: the line
    is appended where the log is."""
    limit = _number("WALMART_GATE_LOG_ROTATE_BYTES", LOG_ROTATE_BYTES)
    try:
        if limit <= 0 or path.stat().st_size <= limit:
            return
    except OSError:
        return
    day = _today().isoformat()
    target = path.with_name(f"{path.stem}.{day}{path.suffix}")
    count = 1
    while target.exists():
        count += 1
        target = path.with_name(f"{path.stem}.{day}.{count}{path.suffix}")
    try:
        os.rename(path, target)
    except OSError:
        return
    for old in _rotated_logs(path)[:-LOG_ROTATED_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_due(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _line_in_tail(path: Path, line: str) -> bool:
    """Read the log back (its tail) and look for the line just written."""
    span = len(line.encode("utf-8")) + 4096
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - span))
        tail = handle.read().decode("utf-8", errors="replace")
    return line in tail  # not "last": another run may have appended after ours


def _log_refusal(reason: str) -> None:
    """A refusal raises inside the caller's interpreter, which may be a robot on another machine; the
    G: log is the one place every machine can read, so the refusal and its reason go there too."""
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} REFUSED: {reason}"
    print(f"[GATE] {line}")
    try:
        _append_line(log_path(), line)
    except OSError as exc:
        _warn_every(f"log-write {log_path()}", f"[WARNING] Could not write the gate log {log_path()}: {exc}")


def _log(label: str, kept: int, facts: dict, retired: set[str]) -> None:
    by_list = ", ".join(f"{name}: {count}" for name, count in facts["by_list"].items())
    _write_log_line(f"{datetime.datetime.now().isoformat(timespec='seconds')} {label}: kept {kept}, "
                    f"dropped {facts['rows_dropped']} retired row(s) = {facts['distinct']} distinct SKU(s) [{by_list}]; "
                    f"rows in {facts['rows_in']}; {facts['blank']} blank SKU row(s) dropped; "
                    f"{_ran_on(retired)}; dropped SKUs: {', '.join(facts['names'])}")


def _write_log_line(line: str) -> None:
    """Print the line, append it to the log and read it back (see the module doc)."""
    print(f"[GATE] {line if len(line) <= 2000 else line[:2000] + ' ...'}")
    log = log_path()
    try:
        _append_line(log, line)
    except OSError as exc:
        _warn_every(f"log-write {log}", f"[WARNING] Could not write the gate log {log}: {exc}")
        return
    try:
        confirmed = _line_in_tail(log, line)
    except OSError:
        confirmed = False
    if not confirmed:
        _warn_every(f"log-readback {log}",
                    f"[WARNING] Gate log line not confirmed: {log} was written but the line is not in it when read back")


def drop_retired_skus(skus: list, retired: set[str], label: str) -> list:
    mask, facts = _partition(skus, retired)
    kept = [s for s, keep in zip(skus, mask) if keep]
    _log(label, len(kept), facts, retired)
    return kept


def drop_retired_rows(frame, sku_column: str, label: str, retired: set[str] | None = None):
    """Rows of a pandas DataFrame whose SKU is neither blank nor retired, paired by position (a
    repeated or unsorted index cannot misalign the mask). Keeps the columns even when no row is left
    (frame[mask] with an empty mask is a column selection)."""
    if retired is None:
        retired = load_retired_skus()
    mask, facts = _partition(frame[sku_column].tolist(), retired)
    kept = frame.loc[mask]
    if len(kept) != len(frame) - facts["rows_dropped"] - facts["blank"]:
        raise RuntimeError(f"{label}: the gate lost track of rows ({len(frame)} in, {len(kept)} kept, "
                           f"{facts['rows_dropped']} retired and {facts['blank']} blank dropped)")
    _log(label, len(kept), facts, retired)
    return kept


def drop_retired_gtin_rows(frame, gtins, label: str, retired: set[str] | None = None, sku_column: str = "sku"):
    """Rows of a pandas DataFrame whose GTIN is not retired, paired by position. `gtins` is the
    column that holds the GTIN each row is sent with, or a sequence of those GTINs, one per row (None
    where the row is sent without one). A row without a GTIN passes. Logs its own line ("dropped N
    by GTIN"), naming each dropped GTIN with the row's SKU."""
    if retired is None:
        retired = load_retired_skus()
    values = frame[gtins].tolist() if isinstance(gtins, str) else list(gtins)
    if len(values) != len(frame):
        raise RuntimeError(f"{label}: {len(values)} GTINs for {len(frame)} rows")
    mask = gtin_keep_mask(values, retired)
    skus = frame[sku_column].tolist() if sku_column in frame.columns else [""] * len(values)
    sources = getattr(retired, "gtin_sources", {})
    dropped: dict[str, str] = {}
    for value, sku, keep in zip(values, skus, mask):
        if not keep:
            dropped.setdefault(canonical_gtin(value), f"{value} ({sku})")
    by_list: dict[str, int] = {}
    for key in dropped:
        for name in sources.get(key, ["unattributed"]):
            by_list[name] = by_list.get(name, 0) + 1
    kept = frame.loc[mask]
    no_gtin = sum(1 for value in values if not canonical_gtin(value))
    _write_log_line(f"{datetime.datetime.now().isoformat(timespec='seconds')} {label}: kept {len(kept)}, "
                    f"dropped {mask.count(False)} by GTIN = {len(dropped)} distinct GTIN(s) "
                    f"[{', '.join(f'{name}: {count}' for name, count in by_list.items())}]; rows in {len(values)}; "
                    f"{no_gtin} row(s) without a GTIN passed; {_ran_on(retired, 'GTIN')}; "
                    f"dropped GTINs: {', '.join(dropped.values())}")
    return kept


# --------------------------------------------------------------------------------------------------
# liveness: python walmart_retired_filter.py --status
# --------------------------------------------------------------------------------------------------

def is_gate_line(line: str) -> bool:
    """A line a gate run wrote (a filter line or a REFUSED line), not an EXCEPTION line the feed HTTP
    helper wrote for a script that raised: a script failing before its gate is not the gate running."""
    return bool(line.strip()) and line.split(" ", 2)[1:2] != ["EXCEPTION"]


def _last_log_age_hours() -> float | None:
    try:
        lines = [l for l in log_path().read_text(encoding="utf-8").splitlines() if is_gate_line(l)]
        stamp = datetime.datetime.fromisoformat(lines[-1].split(" ", 1)[0])
    except (OSError, IndexError, ValueError):
        return None
    return max(0.0, (datetime.datetime.now() - stamp).total_seconds() / 3600)


def status() -> list[str]:
    """What is wrong with the gate right now; empty when healthy. This is the gate's liveness as an
    output: the lists load and the clone is fresh, and a gate run was logged recently. A gate that
    nothing calls (a script reverted, a path changed) raises no exception, so only its log can say so."""
    problems: list[str] = []
    try:
        _load(log_refusal=False)  # a refusal here is the status's finding, not a gate run: never logged
    except RuntimeError as exc:
        problems.append(str(exc))
    limit = _number("WALMART_GATE_LOG_MAX_AGE_HOURS", LOG_MAX_AGE_HOURS)
    age = _last_log_age_hours()
    if age is None:
        problems.append(f"no gate run is logged in {log_path()}; nothing is calling the gate")
    elif limit > 0 and age > limit:
        problems.append(f"no gate run for {age:.1f} h (limit {limit:g} h) in {log_path()}; "
                        "the upload scripts are not calling the gate")
    return problems


def main(argv: list[str]) -> int:
    if argv[:1] != ["--status"]:
        print("usage: walmart_retired_filter.py --status")
        return 2
    problems = status()
    for problem in problems:
        print(f"[PROBLEM] {problem}")
    print("[OK] the gate loads its lists and has run recently" if not problems else f"[FAILED] {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
