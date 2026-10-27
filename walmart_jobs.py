"""Walmart jobs on this PC: the converter's upload batch, the order export, the tracking upload, feed status.

    python walmart_jobs.py export                   main.py through the BAT's worker pass (lock, Bat Walmart Logs.txt)
    python walmart_jobs.py uploads    [--dry-run]   upload the converter's newest batch, once, step by step
    python walmart_jobs.py orders     [--dry-run]   Walmart pending orders to the two CSVs the robot reads
    python walmart_jobs.py tracking   [--dry-run] [--force]   post new tracking rows, at most once an hour
    python walmart_jobs.py feedstatus               check every recorded feed until Walmart finishes it
    python walmart_jobs.py status                   write walmart_jobs_status.txt; exit 0 OK, 3 when the only
                                                    ATTENTION is late orders, 1 for any other ATTENTION
    python walmart_jobs.py seed                     mark the current batch and tracking rows as already sent

The inventory and price export runs main.py through .BAT File Refresh\\Inventory Refresh.bat --worker in a
hidden cmd.exe: the BAT holds the lock and writes Bat Walmart Logs.txt, where `status` reads the exit code.
A task must not use conhost --headless for this: from 2026-10-23 17:50 it exited 0 every hour and never
ran the BAT.

Uploads. The converter (robot, UiPath) writes the five Walmart files and then the marker
"Walmart batch ready.txt" beside them, one key=value per line:

    run_time=2026-10-23T15:40:47
    zero_quantity=1
    update=1
    price=1
    match=1
    unmatched=0
    lag_time=1

A step runs only when its key is 1 (or true/yes), so the converter's sheet switches (Walmart, Matched, Full)
decide here exactly as they did when the converter posted. No marker, or a marker already processed, means
nothing is uploaded. A batch is its run_time. Each step is one child process (walmart_jobs_child.py) that
loads the owner's script by path and calls its entry function with the same file the converter used, in
the converter's order: zero quantity (Delete Merged.csv), update and price (Update Merged.csv), match,
unmatched, lag time (main.py's merged export).

Step outcomes, kept per batch in the state file:
  done                every feed POST was answered 2xx (a dry run: dry_done)
  rejected            a feed POST was answered 4xx or 5xx other than 429: sent, never retried
  sent_unknown        the step failed after a feed POST went out: never retried (it may duplicate a feed)
  failed_before_send  the step failed and nothing was posted, or Walmart answered 429: retried on the next
                      run, at most MAX_ATTEMPTS times, and only while its file is no newer than the marker
  gate_refused        the never relist gate refused: the whole batch stops
  seeded              marked as already sent by `seed` (the robot sent it)
Update and price wait while zero quantity has not completed. A newer marker supersedes an older batch.

Dry run (--dry-run): every child fakes its Walmart POSTs (token requests and GETs still run), the order
export writes nothing, and the state lives in walmart_jobs_state.dry.json, so a shadow run never marks a
real batch, order or shipment as done. Every log line of a dry run carries DRY.

Logs and state live in G:\\Documents And Logs. A child's output is never copied raw (some scripts print the
token reply): only its last lines, with any 32+ character token like run redacted, and only on failure.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHILD = HERE / "walmart_jobs_child.py"
PYTHON = Path(r"C:\Program Files\Python310\python.exe")
LOGS = Path(r"G:\Documents And Logs")
WALMART_DIR = Path(r"G:\Automation Google Drive\Wholesale UI CSVs\Merged Browser Uploads\Walmart")
EXPORT_DIR = Path(r"G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports"
                  r"\API Python Generated Merged Exports")
MERGED_EXPORT = EXPORT_DIR / "Merged Price And Quantity Walmart Export.csv"
PUSHER_LOG = Path(r"C:\Users\Daniel\Downloads\AI\Claude\ShipStation And Veeqo\unmarked-merch-automation\pusher_loop.log")
MARKER_NAME = "Walmart batch ready.txt"
ORDERS_CSV = Path(r"G:\Automation Google Drive\Google Sheet Connected CSVs\Pending Order Exports\Walmart"
                  r"\Raw Walmart Orders With JSON Headers.csv")
AUTO_CANCEL_DAYS = 4  # Walmart auto cancels an order not marked Shipped by its Expected Ship Date plus four days
STATUS_LATE_ONLY = 3  # status's exit code when late orders are its only ATTENTION (2 is "G: unreachable")

STEPS = (  # (marker key, script, entry function, the file it reads)
    ("zero_quantity", "Update Inventory.py", "makescriptnamed", "Delete Merged.csv"),
    ("update", "Update Inventory.py", "makescriptnamed", "Update Merged.csv"),
    ("price", "Update Pricing.py", "make_script_named", "Update Merged.csv"),
    ("match", "New Products By Match.py", "make_new_product_uploads", "New Merged Existing For Match.csv"),
    ("unmatched", "New Products Unmatched.py", "new_products_full_data", "New Merged New For Walmart Unmatched.csv"),
    ("lag_time", "Lag Time Bulk.py", None, None),  # reads main.py's merged export itself, at load
)
WAITS_FOR = {"update": "zero_quantity", "price": "zero_quantity"}
FINAL = {"done", "dry_done", "rejected", "sent_unknown", "seeded", "skipped_off"}
MAX_ATTEMPTS = 3
THROTTLE_WAIT_MIN = 30  # a step Walmart answered 429 waits this long before its next attempt (2026-10-23: lagtime 429 two minutes after the robot's)
TIMEOUTS = {"step": 3600, "orders": 600, "tracking": 1800, "export": 3 * 3600}
BAT = HERE / ".BAT File Refresh" / "Inventory Refresh.bat"
TRACKING_EVERY_S = 55 * 60
KEEP_BATCHES = 48
KEEP_FEED_RESULTS = 300
FEED_PENDING_MAX_H = 24
GATE_REFUSAL = "never relist gate cannot be built"
_SECRET_RUN = re.compile(r"[A-Za-z0-9_\-]{32,}")
CREATE_NO_WINDOW = 0x08000000

ATTENTION_EXPORT_AGE_H = 3
ATTENTION_MARKER_WAIT_MIN = 20
ATTENTION_ORDERS_AGE_MIN = 45
ATTENTION_FEED_FAIL_RATIO = 0.2
ATTENTION_IDLE_H = 2


def redact(text) -> str:
    return _SECRET_RUN.sub("<redacted>", str(text))


def now() -> _dt.datetime:
    return _dt.datetime.now().replace(microsecond=0)


def iso(t: _dt.datetime | None) -> str | None:
    return t.isoformat() if t else None


def parse_iso(text) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


class Paths:
    """Every location the runner touches; tests point it at a temporary directory."""

    def __init__(self, logs: Path = LOGS, walmart_dir: Path = WALMART_DIR, export: Path = MERGED_EXPORT,
                 pusher_log: Path = PUSHER_LOG, project: Path = HERE, orders_csv: Path = ORDERS_CSV):
        self.logs, self.walmart_dir, self.export, self.pusher_log, self.project = logs, walmart_dir, export, pusher_log, project
        self.orders_csv = orders_csv
        self.marker = walmart_dir / MARKER_NAME
        self.jobs_log = logs / "walmart_jobs.log"
        self.status = logs / "walmart_jobs_status.txt"
        self.ledger = logs / "walmart_tracking_posted.csv"
        self.results = logs / "walmart_jobs_results"
        self.bat_log = logs / "Bat Walmart Logs.txt"
        self.gate_log = logs / "walmart_retired_filter.log"

    def state(self, dry: bool) -> Path:
        return self.logs / ("walmart_jobs_state.dry.json" if dry else "walmart_jobs_state.json")


# ------------------------------------------------------------------------------------------ state, log

def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    for key, default in (("batches", {}), ("feeds", {}), ("feed_results", []), ("orders", {}), ("tracking", {}), ("runs", {})):
        state.setdefault(key, default)
    return state


def save_state(path: Path, state: dict) -> None:
    batches = state["batches"]
    for old in sorted(batches)[:-KEEP_BATCHES]:
        del batches[old]
    state["feed_results"] = state["feed_results"][-KEEP_FEED_RESULTS:]
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


class Log:
    def __init__(self, paths: Paths, job: str, dry: bool):
        self.path, self.job, self.dry = paths.jobs_log, job, dry
        self.host = socket.gethostname()

    def line(self, step: str, outcome: str, detail: str = "", tail: list[str] | None = None, batch: str = "") -> None:
        text = f"{iso(now())} {self.host} {'DRY ' if self.dry else ''}{self.job} {batch + ' ' if batch else ''}{step} {outcome}"
        if detail:
            text += f" | {redact(' '.join(str(detail).split()))}"
        lines = [text] + [f"    | {redact(t)}" for t in (tail or [])]
        print("\n".join(lines))
        with self.path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


@contextlib.contextmanager
def job_lock(paths: Paths, job: str):
    """A lock held by an open file handle: released when this process ends, even if it is killed."""
    import msvcrt
    handle = (paths.logs / f"walmart_jobs_{job}.lock").open("a+")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(OSError):
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def wait_for(path: Path, seconds: int = 120) -> bool:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > deadline:
            return False
        time.sleep(5)
    return True


# ------------------------------------------------------------------------------------------ children

class StepResult:
    def __init__(self, outcome: str, detail: str = "", feed_ids: list[str] | None = None, tail: list[str] | None = None):
        self.outcome, self.detail, self.feed_ids, self.tail = outcome, detail, feed_ids or [], tail or []


def run_child(paths: Paths, args: list[str], dry: bool, timeout: int, label: str) -> tuple[int | None, list[dict], str]:
    paths.results.mkdir(parents=True, exist_ok=True)
    results = paths.results / f"{now().strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^A-Za-z0-9]+', '_', label)}.jsonl"
    cmd = [str(PYTHON), "-u", "-X", "utf8", str(CHILD), "--results", str(results)] + (["--dry-run"] if dry else []) + args
    try:
        done = subprocess.run(cmd, cwd=str(paths.project), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW)
        code, output = done.returncode, (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired as exc:
        code = None
        output = ((exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")) + \
                 ((exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
        output += f"\n(timed out after {timeout} s)"
    rows = []
    if results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                rows.append(json.loads(line))
    prune_results(paths)
    return code, rows, output


def prune_results(paths: Paths, keep_days: int = 14) -> None:
    cutoff = time.time() - keep_days * 86400
    for f in paths.results.glob("*.jsonl"):
        with contextlib.suppress(OSError):
            if f.stat().st_mtime < cutoff:
                f.unlink()


def tail_of(output: str, n: int = 15) -> list[str]:
    return [line for line in output.strip().splitlines() if line.strip()][-n:]


def classify(code: int | None, rows: list[dict], output: str, dry: bool) -> StepResult:
    """The outcome of one step from its child's records (see the module doc)."""
    posts = [r for r in rows if r.get("event") == "http" and r.get("method") == "POST" and not r.get("token")]
    sent = [r for r in posts if not r.get("dry") and r.get("status") is not None]
    feed_ids = [r["feedId"] for r in sent if r.get("feedId")]
    end = next((r for r in reversed(rows) if r.get("event") == "end"), {})
    error = end.get("error") or ("" if code == 0 else f"exit {code}")
    if GATE_REFUSAL in output or GATE_REFUSAL in str(error):
        return StepResult("gate_refused", error, feed_ids, tail_of(output))
    bad = [r for r in sent if not (200 <= int(r["status"]) < 300)]
    throttled = [r for r in bad if int(r["status"]) == 429]
    if bad and len(throttled) < len(bad):
        codes = ",".join(str(r["status"]) for r in bad)
        return StepResult("rejected", f"Walmart answered {codes} to {len(bad)} of {len(sent)} feed POSTs", feed_ids, tail_of(output))
    if code == 0 and end.get("ok") and not throttled:
        what = f"{len(posts)} feed POST(s)" + (" faked" if dry else f", feedIds {','.join(feed_ids) or 'none'}")
        return StepResult("dry_done" if dry else "done", what, feed_ids)
    if throttled:
        return StepResult("failed_before_send", f"Walmart answered 429 (throttled) to {len(throttled)} feed POST(s)", feed_ids, tail_of(output))
    if sent or "failed after the request was sent" in str(error) or "failed after the request was sent" in output:
        return StepResult("sent_unknown", error, feed_ids, tail_of(output))
    return StepResult("failed_before_send", error, feed_ids, tail_of(output))


# ------------------------------------------------------------------------------------------ uploads

def read_marker(path: Path) -> dict | None:
    """The marker as {run_time, steps: {key: bool}}; None when absent or without a run_time."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    values = {}
    with contextlib.suppress(ValueError):
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            values = {str(k).strip().lower(): str(v).strip() for k, v in loaded.items()}
    if not values:
        for line in text.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip().lower()] = value.strip()
    run_time = values.get("run_time")
    if not run_time:
        return None
    steps = {key: values.get(key, "0").lower() in ("1", "true", "yes", "on") for key, *_ in STEPS}
    return {"run_time": run_time, "steps": steps, "mtime": path.stat().st_mtime}


def step_file(paths: Paths, name: str | None) -> Path:
    return paths.export if name is None else paths.walmart_dir / name


def fingerprint(path: Path) -> dict:
    try:
        st = path.stat()
    except FileNotFoundError:
        return {"missing": True}
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return {"size": st.st_size, "mtime": st.st_mtime, "sha256": digest.hexdigest()}


def file_ok(path: Path) -> str | None:
    """None when the file exists and ends with a line break, else why not."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return "empty"
            f.seek(-1, os.SEEK_END)
            if f.read(1) not in (b"\n", b"\r"):
                return "does not end with a line break (still being written?)"
    except FileNotFoundError:
        return "missing"
    return None


def new_batch(marker: dict, paths: Paths) -> dict:
    files = {name: fingerprint(step_file(paths, name)) for *_, name in STEPS if name}
    return {"run_time": marker["run_time"], "marker_mtime": marker["mtime"], "found": iso(now()),
            "enabled": marker["steps"], "files": files, "steps": {}}


def uploads(paths: Paths, dry: bool, log: Log, runner=run_child) -> int:
    state_path = paths.state(dry)
    state = load_state(state_path)
    state["runs"]["uploads"] = iso(now())
    marker = read_marker(paths.marker)
    if marker is None:
        save_state(state_path, state)
        return 0
    batch = state["batches"].get(marker["run_time"])
    if batch is None:
        batch = state["batches"][marker["run_time"]] = new_batch(marker, paths)
        log.line("batch", "found", batch=marker["run_time"], detail="steps on: " + ",".join(k for k, on in marker["steps"].items() if on))
    failed = False
    for key, script, entry, name in STEPS:
        record = batch["steps"].setdefault(key, {"outcome": None, "attempts": 0})
        if record["outcome"] in FINAL or record["outcome"] == "gate_refused":
            continue
        if not batch["enabled"].get(key):
            record["outcome"] = "skipped_off"
            continue
        if record["attempts"] >= MAX_ATTEMPTS:
            failed = True
            continue
        not_before = parse_iso(record.get("not_before"))
        if not_before and now() < not_before:
            continue
        waits = WAITS_FOR.get(key)
        if waits and batch["steps"].get(waits, {}).get("outcome") not in FINAL:
            log.line(key, "waiting", f"{waits} has not completed", batch=marker["run_time"])
            continue
        path = step_file(paths, name)
        problem = file_ok(path)
        if not problem and name and path.stat().st_mtime > batch["marker_mtime"] + 5:
            problem = "the file changed after the marker (a newer converter cycle is writing)"
        if problem:
            record["attempts"] += 1
            record["outcome"], record["detail"] = "failed_before_send", f"{path.name}: {problem}"
            log.line(key, "failed_before_send", record["detail"], batch=marker["run_time"])
            failed = True
            continue
        args = ["script", str(paths.project / script)] + ([entry, str(path)] if entry else [])
        record["attempts"] += 1
        code, rows, output = runner(paths, args, dry, TIMEOUTS["step"], f"{key}")
        result = classify(code, rows, output, dry)
        record.update({"outcome": result.outcome, "detail": result.detail, "at": iso(now()), "feed_ids": result.feed_ids})
        if "429" in result.detail and result.outcome == "failed_before_send":
            record["not_before"] = iso(now() + _dt.timedelta(minutes=THROTTLE_WAIT_MIN))
        log.line(key, result.outcome, result.detail, result.tail, batch=marker["run_time"])
        if not dry:
            for feed_id in result.feed_ids:
                state["feeds"][feed_id] = {"step": key, "batch": marker["run_time"], "posted": iso(now())}
        save_state(state_path, state)
        if result.outcome == "gate_refused":
            failed = True
            break
        if result.outcome not in FINAL:
            failed = True
    save_state(state_path, state)
    return 1 if failed else 0


# ------------------------------------------------------------------------------------------ export

def export(paths: Paths, log: Log, bat: Path = BAT) -> int:
    started = iso(now())
    try:
        done = subprocess.run(["cmd.exe", "/c", str(bat), "--worker"], cwd=str(paths.project),
                              timeout=TIMEOUTS["export"], creationflags=CREATE_NO_WINDOW)
        code = done.returncode
    except subprocess.TimeoutExpired:
        code = None
    outcome = {0: "done", 2: "skipped"}.get(code, "failed")
    log.line("export", outcome, f"Inventory Refresh.bat --worker started {started}, exit {code}"
             + (" (another run holds the lock)" if code == 2 else ""))
    state = load_state(paths.state(False))
    state["runs"]["export"] = iso(now())
    save_state(paths.state(False), state)
    return 0 if code in (0, 2) else 1


# ------------------------------------------------------------------------------------------ orders, tracking

def orders(paths: Paths, dry: bool, log: Log, runner=run_child) -> int:
    state_path = paths.state(dry)
    state = load_state(state_path)
    code, rows, output = runner(paths, ["orders"], dry, TIMEOUTS["orders"], "orders")
    end = next((r for r in reversed(rows) if r.get("event") == "end"), {})
    record = state["orders"]
    if code == 0 and end.get("ok"):
        record["last_ok"] = iso(now())
        summary = next((ln for ln in reversed(output.splitlines()) if ln.startswith(("orders:", "DRY RUN"))), "")
        log.line("orders", "dry_done" if dry else "done", summary)
        rc = 0
    else:
        record["last_fail"], record["last_error"] = iso(now()), end.get("error") or f"exit {code}"
        log.line("orders", "failed", record["last_error"], tail_of(output))
        rc = 1
    state["runs"]["orders"] = iso(now())
    save_state(state_path, state)
    return rc


def tracking_csv(paths: Paths) -> Path:
    sys.path.insert(0, str(paths.project))
    import walmart_jobs_child  # noqa: E402  (imports requests; only needed here)
    return walmart_jobs_child.tracking_csv_path()


def tracking(paths: Paths, dry: bool, log: Log, force: bool = False, runner=run_child, csv_path: Path | None = None) -> int:
    state_path = paths.state(dry)
    state = load_state(state_path)
    record = state["tracking"]
    last = parse_iso(record.get("last_attempt"))
    if not force and last and (now() - last).total_seconds() < TRACKING_EVERY_S:
        return 0
    csv_path = csv_path or tracking_csv(paths)
    digest = fingerprint(csv_path).get("sha256")
    if not force and digest and digest == record.get("last_hash") and record.get("last_outcome") == "done":
        record["last_attempt"] = iso(now())
        save_state(state_path, state)
        return 0
    record["last_attempt"] = iso(now())
    code, rows, output = runner(paths, ["tracking", "--ledger", str(paths.ledger)], dry, TIMEOUTS["tracking"], "tracking")
    end = next((r for r in reversed(rows) if r.get("event") == "end"), {})
    summary = next((ln for ln in output.splitlines() if ln.startswith("tracking:")), "")
    if code == 0 and end.get("ok"):
        record.update({"last_hash": digest, "last_outcome": "done", "last_ok": iso(now()), "last_summary": summary})
        log.line("tracking", "dry_done" if dry else "done", summary)
        rc = 0
    else:
        record.update({"last_outcome": "failed", "last_error": end.get("error") or f"exit {code}", "last_fail": iso(now())})
        log.line("tracking", "failed", record["last_error"], tail_of(output))
        rc = 1
    state["runs"]["tracking"] = iso(now())
    save_state(state_path, state)
    return rc


# ------------------------------------------------------------------------------------------ feed status

def feedstatus(paths: Paths, log: Log, fetch=None) -> int:
    state_path = paths.state(False)
    state = load_state(state_path)
    if not state["feeds"]:
        state["runs"]["feedstatus"] = iso(now())
        save_state(state_path, state)
        return 0
    fetch = fetch or _fetch_feed
    rc = 0
    for feed_id, info in list(state["feeds"].items()):
        try:
            data = fetch(feed_id)
        except Exception as exc:
            log.line(info["step"], "feedstatus_error", f"{feed_id}: {type(exc).__name__}: {exc}")
            rc = 1
            continue
        status = str(data.get("feedStatus", "")).upper()
        received, failed = int(data.get("itemsReceived") or 0), int(data.get("itemsFailed") or 0)
        if status in ("PROCESSED", "ERROR"):
            result = {"feedId": feed_id, **info, "feedStatus": status, "itemsReceived": received,
                      "itemsSucceeded": int(data.get("itemsSucceeded") or 0), "itemsFailed": failed, "checked": iso(now())}
            state["feed_results"].append(result)
            del state["feeds"][feed_id]
            bad = status == "ERROR" or (received and failed / received > ATTENTION_FEED_FAIL_RATIO)
            log.line(info["step"], "feed_" + status.lower(), f"{feed_id}: {received} received, {failed} failed")
            rc = rc or (1 if bad else 0)
        elif (now() - (parse_iso(info["posted"]) or now())).total_seconds() > FEED_PENDING_MAX_H * 3600:
            state["feed_results"].append({"feedId": feed_id, **info, "feedStatus": f"STILL {status} AFTER {FEED_PENDING_MAX_H} H",
                                          "checked": iso(now())})
            del state["feeds"][feed_id]
            log.line(info["step"], "feed_stuck", f"{feed_id}: {status} after {FEED_PENDING_MAX_H} h")
            rc = 1
    state["runs"]["feedstatus"] = iso(now())
    save_state(state_path, state)
    return rc


def _fetch_feed(feed_id: str) -> dict:
    sys.path.insert(0, str(HERE))
    import requests
    import walmart_jobs_child
    token = walmart_jobs_child.strict_token()
    headers = {"WM_SEC.ACCESS_TOKEN": token, "WM_QOS.CORRELATION_ID": os.getenv("WM_QOS_CORRELATION_ID"),
               "WM_SVC.NAME": "My Walmart Inventory", "Accept": "application/json"}
    response = requests.get(f"https://marketplace.walmartapis.com/v3/feeds/{feed_id}?includeDetails=false",
                            headers=headers, timeout=(10, 60))
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {redact(response.text[:200])}")
    return response.json()


# ------------------------------------------------------------------------------------------ seed

def seed(paths: Paths, log: Log, runner=run_child) -> int:
    """Mark the batch the robot already sent, and the tracking rows already in the file, as sent."""
    state_path = paths.state(False)
    state = load_state(state_path)
    marker = read_marker(paths.marker)
    if marker:
        batch = state["batches"].setdefault(marker["run_time"], new_batch(marker, paths))
        for key, *_ in STEPS:
            record = batch["steps"].setdefault(key, {"outcome": None, "attempts": 0})
            if record["outcome"] not in FINAL:
                record["outcome"] = "seeded"
        log.line("batch", "seeded", "every step of the current batch marked as already sent", batch=marker["run_time"])
    else:
        log.line("uploads", "seed", "no marker yet: nothing to mark")
    code, rows, output = runner(paths, ["tracking", "--ledger", str(paths.ledger), "--seed"], False, TIMEOUTS["tracking"], "tracking_seed")
    summary = next((ln for ln in output.splitlines() if ln.startswith("tracking:")), output.strip()[-200:])
    log.line("tracking", "seeded" if code == 0 else "seed_failed", summary)
    if code == 0:
        state["tracking"].update({"last_outcome": "done", "last_attempt": iso(now())})
        with contextlib.suppress(Exception):
            state["tracking"]["last_hash"] = fingerprint(tracking_csv(paths)).get("sha256")
    save_state(state_path, state)
    return 0 if code == 0 else 1


# ------------------------------------------------------------------------------------------ status

_BAT_FINISHED = re.compile(r"^Finished at \w{3} (\d{2}/\d{2}/\d{4}) (\d{1,2}:\d{2}:\d{2})\.\d+ with exit code (-?\d+)(?: on (\S+))?")


def last_export_run(bat_log: Path, host: str | None = None) -> tuple[_dt.datetime | None, int | None]:
    """The last run's finish time and exit code: the last one this host ran (the BAT names the computer since
    2026-10-23), else the last one that names no computer. Runs by other computers (the robot) are ignored."""
    host = (host or socket.gethostname()).lower()
    try:
        with bat_log.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400_000))
            text = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None, None
    for line in reversed(text.splitlines()):
        m = _BAT_FINISHED.match(line.strip())
        if m and (m.group(4) is None or m.group(4).lower() == host):
            when = _dt.datetime.strptime(f"{m.group(1)} {m.group(2).zfill(8)}", "%m/%d/%Y %H:%M:%S")
            return when, int(m.group(3))
    return None, None


def last_line_containing(path: Path, needle: str, window: int = 2_000_000) -> str | None:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - window))
            text = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None
    return next((line for line in reversed(text.splitlines()) if needle in line), None)


def late_orders(orders_csv: Path, t: _dt.datetime) -> dict | None:
    """Pending orders (Created or Acknowledged: not yet marked Shipped) whose Expected Ship Date has passed,
    from the order export. Counts only: the file holds customer data."""
    import csv
    try:
        with orders_csv.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return None
    ship_by = {}
    for r in rows:
        with contextlib.suppress(KeyError, ValueError, TypeError):
            ship_by[r["purchaseOrderId"]] = _dt.datetime.fromtimestamp(int(r["estimatedShipDate"]) / 1000)
    late = sorted(d for d in ship_by.values() if d < t)
    soon = [d for d in ship_by.values() if t <= d < t + _dt.timedelta(hours=24)]
    first = (late[0] + _dt.timedelta(days=AUTO_CANCEL_DAYS)).strftime("%Y-%m-%d %H:%M") if late else None
    return {"pending": len(ship_by), "late": len(late), "soon": len(soon), "first_cancel": first}


def status(paths: Paths, log: Log | None = None) -> int:
    t = now()
    live, dry = load_state(paths.state(False)), load_state(paths.state(True))
    attention, lines = [], [f"walmart jobs status written {iso(t)} on {socket.gethostname()}"]

    for name in ("Walmart_Authorization", "WM_QOS_CORRELATION_ID"):
        if not os.getenv(name):
            attention.append(f"{name} is not set in this session")

    when, code = last_export_run(paths.bat_log)
    export_age = (t.timestamp() - paths.export.stat().st_mtime) / 3600 if paths.export.exists() else None
    lines.append(f"export: last run finished {iso(when) or 'never'} with exit code {code}; "
                 f"merged export {'missing' if export_age is None else f'{export_age:.1f} h old'}")
    if code not in (0, None):
        attention.append(f"the last export run exited {code} (see Bat Walmart Logs.txt)")
    if export_age is None or export_age > ATTENTION_EXPORT_AGE_H:
        attention.append(f"the merged export is older than {ATTENTION_EXPORT_AGE_H} h")

    marker = read_marker(paths.marker)
    for label, state in (("live", live), ("dry", dry)):
        if not state["batches"]:
            lines.append(f"uploads ({label}): no batch yet")
            continue
        run_time = sorted(state["batches"])[-1]
        steps = state["batches"][run_time]["steps"]
        lines.append(f"uploads ({label}): batch {run_time}: " +
                     ", ".join(f"{k} {v.get('outcome')}" for k, v in steps.items()))
        if label == "live":
            for k, v in steps.items():
                if v.get("outcome") in ("rejected", "sent_unknown", "gate_refused") or \
                        (v.get("outcome") == "failed_before_send" and v.get("attempts", 0) >= MAX_ATTEMPTS):
                    attention.append(f"upload step {k} of batch {run_time}: {v.get('outcome')} ({v.get('detail', '')[:160]})")
    if marker and live["runs"].get("uploads"):
        waiting = marker["run_time"] not in live["batches"]
        age_min = (t.timestamp() - marker["mtime"]) / 60
        if waiting and age_min > ATTENTION_MARKER_WAIT_MIN:
            attention.append(f"the converter's batch {marker['run_time']} has waited {age_min:.0f} min")
    elif not marker:
        lines.append("uploads: no marker file yet (the converter does not write one)")

    for label, state in (("live", live), ("dry", dry)):
        o = state["orders"]
        if o:
            lines.append(f"orders ({label}): last ok {o.get('last_ok')}, last failure {o.get('last_fail')} {o.get('last_error') or ''}".rstrip())
    ok = parse_iso(live["orders"].get("last_ok"))
    if live["runs"].get("orders") and (not ok or (t - ok).total_seconds() > ATTENTION_ORDERS_AGE_MIN * 60):
        attention.append(f"no successful order export in {ATTENTION_ORDERS_AGE_MIN} min")

    for label, state in (("live", live), ("dry", dry)):
        tr = state["tracking"]
        if tr:
            lines.append(f"tracking ({label}): {tr.get('last_outcome')} at {tr.get('last_ok') or tr.get('last_fail')}; {tr.get('last_summary', '')}")
    if live["tracking"].get("last_outcome") == "failed":
        attention.append(f"tracking upload failed: {live['tracking'].get('last_error', '')[:160]}")

    recent = [r for r in live["feed_results"] if (parse_iso(r.get("checked")) or t) > t - _dt.timedelta(hours=24)]
    lines.append(f"feeds: {len(live['feeds'])} awaiting Walmart, {len(recent)} finished in 24 h")
    for r in recent:
        received, failed = r.get("itemsReceived") or 0, r.get("itemsFailed") or 0
        if not str(r.get("feedStatus")).startswith("PROCESSED") or (received and failed / received > ATTENTION_FEED_FAIL_RATIO):
            attention.append(f"feed {r['feedId']} ({r.get('step')}): {r.get('feedStatus')}, {failed} of {received} items failed")

    runs = [parse_iso(v) for v in list(live["runs"].values()) + list(dry["runs"].values())]
    runs = [r for r in runs if r]
    if runs and (t - max(runs)).total_seconds() > ATTENTION_IDLE_H * 3600:
        attention.append(f"no Walmart job has run for {ATTENTION_IDLE_H} h (is Daniel logged on?)")
    if dry["runs"] and (not live["runs"] or max(dry["runs"].values()) > max(live["runs"].values())):
        lines.append("MODE: DRY RUN (shadow): nothing is being sent to Walmart by these jobs")

    late = late_orders(paths.orders_csv, t)
    late_attention = None  # the late order line, when there is one: alone it is exit 3, not 1
    if late is not None:
        lines.append(f"orders past Expected Ship Date: {late['late']} of {late['pending']} pending; {late['soon']} more due within 24 h")
        if late["late"]:
            late_attention = (f"{late['late']} Walmart order(s) past their Expected Ship Date without tracking; Walmart auto "
                              f"cancels at {AUTO_CANCEL_DAYS} days past it; earliest cancel about {late['first_cancel']}")
            attention.append(late_attention)

    store = last_line_containing(paths.pusher_log, "store check Walmart:")
    lines.append(f"ShipStation pusher: {store.strip()[:300] if store else 'no Walmart store check line found'}")
    gate = last_line_containing(paths.gate_log, " Update Inventory: ", window=400_000)
    lines.append(f"gate: last Update Inventory line {gate[:19] if gate else 'none'}")

    if not attention:
        verdict, rc = "OK", 0
    elif attention == [late_attention]:
        verdict, rc = "ATTENTION (late orders only)", STATUS_LATE_ONLY
    else:
        verdict, rc = "ATTENTION", 1
    text = "\n".join(lines + [f"  [ATTENTION] {a}" for a in attention] + [f"verdict: {verdict}"]) + "\n"
    temp = paths.status.with_name(paths.status.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, paths.status)
    print(text, end="")
    return rc


# ------------------------------------------------------------------------------------------ main

def main(argv=None, paths: Paths | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job", choices=["export", "uploads", "orders", "tracking", "feedstatus", "status", "seed"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="tracking: run now even within the hour")
    args = parser.parse_args(argv)
    paths = paths or Paths()
    if not wait_for(paths.logs):
        print(f"[FAILED] {paths.logs} is not reachable (is G: connected?)", file=sys.stderr)
        return 2
    dry = args.dry_run and args.job in ("uploads", "orders", "tracking")
    log = Log(paths, args.job, dry)
    with job_lock(paths, args.job + ("_dry" if dry else "")) as got:
        if not got:
            log.line(args.job, "skipped", "already running")
            return 0
        try:
            if args.job == "export":
                return export(paths, log)
            if args.job == "uploads":
                return uploads(paths, dry, log)
            if args.job == "orders":
                return orders(paths, dry, log)
            if args.job == "tracking":
                return tracking(paths, dry, log, force=args.force)
            if args.job == "feedstatus":
                return feedstatus(paths, log)
            if args.job == "seed":
                return seed(paths, log)
            return status(paths, log)
        except Exception as exc:
            log.line(args.job, "crashed", f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    sys.exit(main())
