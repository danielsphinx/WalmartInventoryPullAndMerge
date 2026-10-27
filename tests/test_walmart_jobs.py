"""Tests for walmart_jobs.py and walmart_jobs_child.py. Nothing here reaches Walmart or the production files:
every HTTP call is a fake, every path is under tmp_path, and the two child hooks (ORDERS_OUTPUT_DIR,
TRACKING_CSV) point the order and tracking modes at tmp_path.

Run from the project folder:  "<compliance clone>\\.venv\\Scripts\\python.exe" -m pytest -q tests
"""
import csv
import json
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import walmart_jobs as jobs  # noqa: E402
import walmart_jobs_child as child  # noqa: E402


# ------------------------------------------------------------------------------------------ helpers

class Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code, self.text, self._payload = status, text, payload
        self.headers = {}

    def json(self):
        return self._payload if self._payload is not None else json.loads(self.text)


def make_paths(tmp_path):
    logs, walmart = tmp_path / "logs", tmp_path / "walmart"
    logs.mkdir()
    walmart.mkdir()
    export = tmp_path / "export.csv"
    export.write_text("sku,availToSellQty\nA,1\n", encoding="utf-8")
    return jobs.Paths(logs=logs, walmart_dir=walmart, export=export, pusher_log=tmp_path / "pusher.log", project=PROJECT,
                      orders_csv=tmp_path / "orders.csv")


def write_batch(paths, run_time="2026-10-23T15:40:47", **steps):
    for *_, name in jobs.STEPS:
        if name:
            (paths.walmart_dir / name).write_text("sku,quantity\nA,0\n", encoding="utf-8")
    on = {key: 1 for key, *_ in jobs.STEPS}
    on.update(steps)
    time.sleep(0.05)
    paths.marker.write_text(f"run_time={run_time}\n" + "".join(f"{k}={v}\n" for k, v in on.items()), encoding="utf-8")


def ok_rows(feed_id="F1", status=202, dry=False):
    return [{"event": "http", "method": "POST", "token": True, "status": 200},
            {"event": "http", "method": "POST", "token": False, "status": status, "feedId": feed_id, "dry": dry},
            {"event": "end", "ok": True, "error": None}]


class FakeRunner:
    """Answers each child call from a per step script of (code, rows, output)."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, paths, args, dry, timeout, label):
        self.calls.append((label, args, dry))
        answer = self.answers.get(label)
        if isinstance(answer, list):
            answer = answer.pop(0) if answer else None
        return answer or (0, ok_rows(feed_id=f"F-{label}", dry=dry), "")


# ------------------------------------------------------------------------------------------ recorder

def test_dry_run_fakes_feed_posts_and_records_them(tmp_path):
    results = tmp_path / "r.jsonl"
    rec = child.Recorder(results, dry_run=True)
    rec._orig["post"] = lambda *a, **k: pytest.fail("a dry run must not send a feed POST")
    r = rec.call("POST", "https://marketplace.walmartapis.com/v3/feeds?feedType=inventory&x=1", rec._orig["post"], (), {"data": "abc"})
    assert r.status_code == 202 and r.json()["feedId"].startswith("DRYRUN-")
    row = json.loads(results.read_text().splitlines()[0])
    assert row["dry"] and row["url"] == "/v3/feeds?feedType=inventory" and row["bytes"] == 3


def test_dry_run_still_sends_token_requests_and_gets(tmp_path):
    rec = child.Recorder(tmp_path / "r.jsonl", dry_run=True)
    seen = []
    real = lambda url, *a, **k: seen.append((url, k.get("timeout"))) or Resp(200, "<x/>")  # noqa: E731
    rec.call("POST", "https://marketplace.walmartapis.com/v3/token", real, (), {})
    rec.call("GET", "https://marketplace.walmartapis.com/v3/orders?limit=1", real, (), {})
    assert [s[1] for s in seen] == [(10, 60), (10, 300)]


def test_live_post_records_status_and_feed_id_never_headers(tmp_path):
    results = tmp_path / "r.jsonl"
    rec = child.Recorder(results, dry_run=False)
    real = lambda url, *a, **k: Resp(202, '{"feedId":"18A2B@AQMBAgA"}')  # noqa: E731
    rec.call("POST", "https://marketplace.walmartapis.com/v3/feeds?feedType=price", real, (),
             {"headers": {"WM_SEC.ACCESS_TOKEN": "SECRET"}, "data": "x", "timeout": (1, 2)})
    text = results.read_text()
    row = json.loads(text)
    assert row["feedId"] == "18A2B@AQMBAgA" and row["status"] == 202 and "SECRET" not in text


def test_script_mode_runs_entry_with_its_file(tmp_path):
    script = tmp_path / "fake.py"
    out = tmp_path / "called.txt"
    script.write_text(f"def go(p):\n    open(r'{out}', 'w').write(p)\n", encoding="utf-8")
    rc = child.main(["--results", str(tmp_path / "r.jsonl"), "--dry-run", "script", str(script), "go", "THE.csv"])
    assert rc == 0 and out.read_text() == "THE.csv"
    end = json.loads((tmp_path / "r.jsonl").read_text().splitlines()[-1])
    assert end == {**end, "event": "end", "ok": True}


def test_script_mode_failure_is_recorded_and_exit_1(tmp_path):
    script = tmp_path / "bad.py"
    script.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    rc = child.main(["--results", str(tmp_path / "r.jsonl"), "script", str(script)])
    end = json.loads((tmp_path / "r.jsonl").read_text().splitlines()[-1])
    assert rc == 1 and not end["ok"] and "boom" in end["error"]


# ------------------------------------------------------------------------------------------ classify

@pytest.mark.parametrize("code,rows,output,dry,expected", [
    (0, ok_rows(), "", False, "done"),
    (0, ok_rows(dry=True), "", True, "dry_done"),
    (0, ok_rows(status=400), "", False, "rejected"),
    (0, ok_rows(status=429), "", False, "failed_before_send"),
    (1, [{"event": "end", "ok": False, "error": "RuntimeError: token"}], "", False, "failed_before_send"),
    (1, ok_rows()[:2] + [{"event": "end", "ok": False, "error": "RuntimeError: second batch"}], "", False, "sent_unknown"),
    (1, [{"event": "end", "ok": False, "error": "RuntimeError: walmart_feed_http: feed POST failed after the request was sent"}], "", False, "sent_unknown"),
    (1, [{"event": "end", "ok": False, "error": "RuntimeError: The never relist gate cannot be built: stale"}], "", False, "gate_refused"),
    (None, [], "(timed out)", False, "failed_before_send"),
])
def test_classify(code, rows, output, dry, expected):
    assert jobs.classify(code, rows, output, dry).outcome == expected


# ------------------------------------------------------------------------------------------ marker

def test_marker_key_values_json_and_missing_run_time(tmp_path):
    m = tmp_path / "m.txt"
    m.write_text("run_time=2026-10-23T15:40:47\nzero_quantity=1\nupdate=true\nmatch=0\n", encoding="utf-8-sig")
    got = jobs.read_marker(m)
    assert got["run_time"] == "2026-10-23T15:40:47"
    assert got["steps"]["zero_quantity"] and got["steps"]["update"] and not got["steps"]["match"] and not got["steps"]["lag_time"]
    m.write_text(json.dumps({"run_time": "T2", "price": "yes"}), encoding="utf-8")
    assert jobs.read_marker(m)["steps"]["price"]
    m.write_text("zero_quantity=1\n", encoding="utf-8")
    assert jobs.read_marker(m) is None
    assert jobs.read_marker(tmp_path / "none.txt") is None


# ------------------------------------------------------------------------------------------ uploads

def test_no_marker_uploads_nothing(tmp_path):
    paths = make_paths(tmp_path)
    runner = FakeRunner()
    assert jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), runner) == 0
    assert runner.calls == []


def test_batch_runs_every_step_in_order_once(tmp_path):
    paths = make_paths(tmp_path)
    write_batch(paths)
    runner = FakeRunner()
    log = jobs.Log(paths, "uploads", False)
    assert jobs.uploads(paths, False, log, runner) == 0
    assert [c[0] for c in runner.calls] == ["zero_quantity", "update", "price", "match", "unmatched", "lag_time"]
    zero_args = runner.calls[0][1]
    assert zero_args[1].endswith("Update Inventory.py") and zero_args[2] == "makescriptnamed" and zero_args[3].endswith("Delete Merged.csv")
    assert runner.calls[2][1][2:] == ["make_script_named", str(paths.walmart_dir / "Update Merged.csv")]
    assert runner.calls[5][1] == ["script", str(PROJECT / "Lag Time Bulk.py")]
    state = jobs.load_state(paths.state(False))
    assert set(state["feeds"]) == {f"F-{k}" for k, *_ in jobs.STEPS}
    jobs.uploads(paths, False, log, runner)
    assert len(runner.calls) == 6, "a processed batch is never uploaded again"


def test_switched_off_steps_are_skipped(tmp_path):
    paths = make_paths(tmp_path)
    write_batch(paths, match=0, unmatched=0)
    runner = FakeRunner()
    jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), runner)
    assert [c[0] for c in runner.calls] == ["zero_quantity", "update", "price", "lag_time"]
    steps = jobs.load_state(paths.state(False))["batches"]["2026-10-23T15:40:47"]["steps"]
    assert steps["match"]["outcome"] == steps["unmatched"]["outcome"] == "skipped_off"


def test_failure_before_send_retries_and_update_waits_for_zero(tmp_path):
    paths = make_paths(tmp_path)
    write_batch(paths)
    fail = (1, [{"event": "end", "ok": False, "error": "RuntimeError: token 500"}], "")
    runner = FakeRunner({"zero_quantity": [fail, None]})
    log = jobs.Log(paths, "uploads", False)
    assert jobs.uploads(paths, False, log, runner) == 1
    assert [c[0] for c in runner.calls] == ["zero_quantity", "match", "unmatched", "lag_time"]
    assert jobs.uploads(paths, False, log, runner) == 0
    assert [c[0] for c in runner.calls[4:]] == ["zero_quantity", "update", "price"]


def test_attempts_are_capped(tmp_path):
    paths = make_paths(tmp_path)
    write_batch(paths, update=0, price=0, match=0, unmatched=0, lag_time=0)
    fail = (1, [{"event": "end", "ok": False, "error": "RuntimeError: x"}], "")
    runner = FakeRunner({"zero_quantity": [fail] * 5})
    log = jobs.Log(paths, "uploads", False)
    for _ in range(5):
        jobs.uploads(paths, False, log, runner)
    assert len(runner.calls) == jobs.MAX_ATTEMPTS


def test_sent_steps_are_never_retried(tmp_path):
    paths = make_paths(tmp_path)
    write_batch(paths, update=0, price=0, match=0, unmatched=0, lag_time=0)
    unknown = (1, ok_rows()[:2] + [{"event": "end", "ok": False, "error": "x"}], "")
    runner = FakeRunner({"zero_quantity": [unknown, None]})
    log = jobs.Log(paths, "uploads", False)
    jobs.uploads(paths, False, log, runner)
    jobs.uploads(paths, False, log, runner)
    assert len(runner.calls) == 1


def test_gate_refusal_stops_the_batch(tmp_path):
    paths = make_paths(tmp_path)
    write_batch(paths)
    refused = (1, [{"event": "end", "ok": False, "error": "RuntimeError: The never relist gate cannot be built: x"}], "")
    runner = FakeRunner({"zero_quantity": [refused]})
    assert jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), runner) == 1
    assert [c[0] for c in runner.calls] == ["zero_quantity"]


def test_a_file_newer_than_the_marker_is_not_uploaded(tmp_path):
    paths = make_paths(tmp_path)
    write_batch(paths, update=0, price=0, match=0, unmatched=0, lag_time=0)
    later = paths.marker.stat().st_mtime + 60
    os.utime(paths.walmart_dir / "Delete Merged.csv", (later, later))
    runner = FakeRunner()
    jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), runner)
    assert runner.calls == []
    assert jobs.load_state(paths.state(False))["batches"]["2026-10-23T15:40:47"]["steps"]["zero_quantity"]["outcome"] == "failed_before_send"


def test_a_cut_off_file_is_not_uploaded(tmp_path):
    paths = make_paths(tmp_path)
    write_batch(paths, update=0, price=0, match=0, unmatched=0, lag_time=0)
    (paths.walmart_dir / "Delete Merged.csv").write_bytes(b"sku,quantity\nA,0")
    os.utime(paths.walmart_dir / "Delete Merged.csv", (paths.marker.stat().st_mtime,) * 2)
    runner = FakeRunner()
    jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), runner)
    assert runner.calls == []


def test_dry_run_state_is_separate_and_marks_nothing_live(tmp_path):
    paths = make_paths(tmp_path)
    write_batch(paths)
    runner = FakeRunner()
    jobs.uploads(paths, True, jobs.Log(paths, "uploads", True), runner)
    assert all(c[2] for c in runner.calls)
    assert not paths.state(False).exists()
    dry = jobs.load_state(paths.state(True))
    assert dry["feeds"] == {}
    assert "DRY uploads" in paths.jobs_log.read_text()
    live = FakeRunner()
    jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), live)
    assert len(live.calls) == 6


def test_seed_marks_the_current_batch_sent(tmp_path):
    paths = make_paths(tmp_path)
    write_batch(paths)
    log = jobs.Log(paths, "seed", False)
    seeded = FakeRunner({"tracking_seed": [(0, [{"event": "end", "ok": True}], "tracking: 1 rows")]})
    jobs.seed(paths, log, seeded)
    runner = FakeRunner()
    jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), runner)
    assert runner.calls == []


# ------------------------------------------------------------------------------------------ orders

ORDER = {
    "purchaseOrderId": "1080000000001", "customerOrderId": "2000000000001", "customerEmailId": "x@relay.walmart.com",
    "orderDate": 1761200000000,
    "shippingInfo": {"phone": "5555550100", "estimatedDeliveryDate": 1761800000000, "estimatedShipDate": 1761400000000,
                     "methodCode": "Standard",
                     "postalAddress": {"name": "Pat Doe", "address1": "1 Main St", "address2": "Apt 2", "city": "Town",
                                       "state": "NY", "postalCode": "10001", "country": "USA"}},
    "orderLines": {"orderLine": [
        {"lineNumber": "1", "item": {"productName": "Album, \"Deluxe\" LP", "sku": "AMS-123", "condition": "New"},
         "charges": {"charge": [{"chargeType": "PRODUCT", "chargeName": "ItemPrice",
                                 "chargeAmount": {"currency": "USD", "amount": 24.99},
                                 "tax": {"taxName": "Tax1", "taxAmount": {"currency": "USD", "amount": 2.1}}}]},
         "orderLineQuantity": {"unitOfMeasurement": "EACH", "amount": "1"}, "statusDate": 1761200000001,
         "orderLineStatuses": {"orderLineStatus": [{"status": "Acknowledged"}]},
         "fulfillment": {"fulfillmentOption": "S2H", "shipMethod": "STANDARD", "pickUpDateTime": 1761300000000,
                         "shippingProgramType": ""}},
        {"lineNumber": "2", "item": {"productName": "Second", "sku": "AMS-456", "condition": "New"},
         "charges": {"charge": [{"chargeType": "PRODUCT", "chargeName": "ItemPrice",
                                 "chargeAmount": {"currency": "USD", "amount": 9.5}}]},
         "orderLineQuantity": {"unitOfMeasurement": "EACH", "amount": "2"},
         "orderLineStatuses": {"orderLineStatus": [{"status": "Created"}]}, "fulfillment": {}}]},
}


def orders_reply(url):
    if "status=Created" in url:
        return Resp(200, payload={"list": {"elements": {"order": [ORDER]}, "meta": {}}})
    return Resp(200, payload={"list": {"elements": {"order": [ORDER]}, "meta": {}}})  # the same order in both: deduplicated


def original_get_orders_output(out_dir: Path, monkeypatch):
    """What Get Orders.py itself writes for the same replies: its own functions, its own open, a fake requests."""
    ns = child.functions_of(child.GET_ORDERS)
    fake = type("R", (), {"get": staticmethod(lambda url, *a, **k: orders_reply(url)),
                          "post": staticmethod(lambda *a, **k: pytest.fail("token is faked"))})
    ns["requests"] = fake
    ns["get_token"] = lambda: "TOK"
    ns["open"] = lambda file, mode="r", *a, **k: open(out_dir / Path(file).name, mode, *a, **k)
    ns["get_json_header_csv"]()
    ns["map_to_second_format"]()


def test_orders_writes_exactly_what_get_orders_writes(tmp_path, monkeypatch):
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    mine.mkdir()
    theirs.mkdir()
    original_get_orders_output(theirs, monkeypatch)
    calls = []
    monkeypatch.setattr(child, "ORDERS_OUTPUT_DIR", mine)
    monkeypatch.setattr(child, "strict_token", lambda: "TOK")
    monkeypatch.setattr(child.requests, "get", lambda url, *a, **k: calls.append(url) or orders_reply(url))
    monkeypatch.setenv("Walmart_Authorization", "x")
    monkeypatch.setenv("WM_QOS_CORRELATION_ID", "y")
    child.run_orders(dry_run=False)
    names = sorted(p.name for p in theirs.iterdir())
    assert names == sorted(p.name for p in mine.iterdir()) and len(names) == 2
    for name in names:
        assert (mine / name).read_bytes() == (theirs / name).read_bytes()
    assert len(calls) == 2, "fetched once: one GET per status, not two passes"


def test_orders_failure_keeps_the_old_files(tmp_path, monkeypatch):
    target = tmp_path / "Raw Walmart Orders With JSON Headers.csv"
    target.write_text("OLD", encoding="utf-8")
    monkeypatch.setattr(child, "ORDERS_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(child, "strict_token", lambda: "TOK")
    monkeypatch.setattr(child.requests, "get", lambda url, *a, **k: Resp(500, "down"))
    monkeypatch.setenv("Walmart_Authorization", "x")
    monkeypatch.setenv("WM_QOS_CORRELATION_ID", "y")
    with pytest.raises(RuntimeError, match="HTTP 500"):
        child.run_orders(dry_run=False)
    assert target.read_text() == "OLD"
    assert not list(tmp_path.glob("~$*"))


def test_orders_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(child, "ORDERS_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(child, "strict_token", lambda: "TOK")
    monkeypatch.setattr(child.requests, "get", lambda url, *a, **k: orders_reply(url))
    monkeypatch.setenv("Walmart_Authorization", "x")
    monkeypatch.setenv("WM_QOS_CORRELATION_ID", "y")
    child.run_orders(dry_run=True)
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------------------------------ tracking

TRACK_FIELDS = ["purchaseOrderId", "customerOrderId", "lineNumber", "orderLineAmount", "methodCode", "carrier",
                "trackingNumber", "trackingURL"]


def write_tracking(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=TRACK_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TRACK_FIELDS})


def real(po, track="1Z999", line="1"):
    return {"purchaseOrderId": po, "customerOrderId": "C" + po, "lineNumber": line, "orderLineAmount": "1",
            "methodCode": "Standard", "carrier": "UPS", "trackingNumber": track, "trackingURL": "https://t/" + track}


def run_tracking(tmp_path, monkeypatch, rows, dry=False, status=200):
    csv_path, ledger = tmp_path / "track.csv", tmp_path / "ledger.csv"
    write_tracking(csv_path, rows)
    monkeypatch.setattr(child, "TRACKING_CSV", csv_path)
    monkeypatch.setattr(child, "strict_token", lambda: "TOK")
    monkeypatch.setenv("Walmart_Authorization", "x")
    monkeypatch.setenv("WM_QOS_CORRELATION_ID", "y")
    posted = []
    rec = child.Recorder(tmp_path / "r.jsonl", dry_run=dry)
    rec._orig["post"] = lambda url, *a, **k: posted.append((url, k.get("json"))) or Resp(status, "{}")
    monkeypatch.setattr(child.requests, "post", lambda url, *a, **k: rec.call("POST", url, rec._orig["post"], a, k))
    return csv_path, ledger, posted, rec


def test_tracking_skips_placeholders_and_posts_real_rows(tmp_path, monkeypatch):
    rows = [{"purchaseOrderId": "#N/A"}, {"purchaseOrderId": ""}, real("108A", track=""), real("108B")]
    _, ledger, posted, rec = run_tracking(tmp_path, monkeypatch, rows)
    child.run_tracking(rec, ledger, dry_run=False)
    assert len(posted) == 1 and posted[0][0].endswith("/v3/orders/108B/shipping")
    line = posted[0][1]["orderShipment"]["orderLines"]["orderLine"][0]
    info = line["orderLineStatuses"]["orderLineStatus"][0]["trackingInfo"]
    assert info["trackingNumber"] == "1Z999" and info["carrierName"]["carrier"] == "UPS" and line["lineNumber"] == "1"
    assert list(csv.DictReader(ledger.open()))[0]["status"] == "200"


def test_tracking_never_posts_a_row_twice(tmp_path, monkeypatch):
    _, ledger, posted, rec = run_tracking(tmp_path, monkeypatch, [real("108B")])
    child.run_tracking(rec, ledger, dry_run=False)
    child.run_tracking(rec, ledger, dry_run=False)
    assert len(posted) == 1


def test_tracking_failure_raises_and_is_retried(tmp_path, monkeypatch):
    _, ledger, posted, rec = run_tracking(tmp_path, monkeypatch, [real("108B")], status=400)
    with pytest.raises(RuntimeError, match="1 of 1 rows failed"):
        child.run_tracking(rec, ledger, dry_run=False)
    with pytest.raises(RuntimeError):
        child.run_tracking(rec, ledger, dry_run=False)
    assert len(posted) == 2, "a 4xx row is not in the posted set, so it is tried again"


def test_tracking_dry_run_sends_nothing_and_writes_no_ledger(tmp_path, monkeypatch):
    _, ledger, posted, rec = run_tracking(tmp_path, monkeypatch, [real("108B")], dry=True)
    child.run_tracking(rec, ledger, dry_run=True)
    assert posted == [] and not ledger.exists()


def test_tracking_seed_fills_the_ledger_without_posting(tmp_path, monkeypatch):
    _, ledger, posted, rec = run_tracking(tmp_path, monkeypatch, [real("108B"), {"purchaseOrderId": "#N/A"}])
    child.run_tracking(rec, ledger, dry_run=False, seed=True)
    child.run_tracking(rec, ledger, dry_run=False)
    assert posted == [] and [r["status"] for r in csv.DictReader(ledger.open())] == ["seeded"]


def test_tracking_job_runs_at_most_hourly(tmp_path):
    paths = make_paths(tmp_path)
    csv_path = tmp_path / "t.csv"
    csv_path.write_text("purchaseOrderId\n#N/A\n", encoding="utf-8")
    runner = FakeRunner({"tracking": [(0, [{"event": "end", "ok": True}], "tracking: 1 rows")] * 3})
    log = jobs.Log(paths, "tracking", False)
    jobs.tracking(paths, False, log, runner=runner, csv_path=csv_path)
    jobs.tracking(paths, False, log, runner=runner, csv_path=csv_path)
    assert len(runner.calls) == 1


# ------------------------------------------------------------------------------------------ feed status, status

def test_feedstatus_finishes_and_flags_errors(tmp_path):
    paths = make_paths(tmp_path)
    state = jobs.load_state(paths.state(False))
    state["feeds"] = {"A": {"step": "update", "batch": "b", "posted": jobs.iso(jobs.now())},
                      "B": {"step": "price", "batch": "b", "posted": jobs.iso(jobs.now())},
                      "C": {"step": "match", "batch": "b", "posted": jobs.iso(jobs.now())}}
    jobs.save_state(paths.state(False), state)
    answers = {"A": {"feedStatus": "PROCESSED", "itemsReceived": 10, "itemsSucceeded": 10, "itemsFailed": 0},
               "B": {"feedStatus": "ERROR", "itemsReceived": 5, "itemsFailed": 5},
               "C": {"feedStatus": "INPROGRESS"}}
    rc = jobs.feedstatus(paths, jobs.Log(paths, "feedstatus", False), fetch=lambda fid: answers[fid])
    state = jobs.load_state(paths.state(False))
    assert rc == 1 and set(state["feeds"]) == {"C"} and [r["feedId"] for r in state["feed_results"]] == ["A", "B"]


def test_bat_log_exit_code_is_read(tmp_path):
    log = tmp_path / "bat.txt"
    log.write_text("Starting Script at Fri 10/23/2026 13:25:29.57\nFinished at Fri 10/23/2026 14:00:48.43 with exit code 0\n"
                   "Starting Script at Fri 10/23/2026 14:59:06.10\nFinished at Fri 10/23/2026 14:59:08.16 with exit code 1\n",
                   encoding="utf-8")
    when, code = jobs.last_export_run(log)
    assert code == 1 and when.hour == 14 and when.minute == 59


def test_status_flags_a_failed_export_and_a_rejected_step(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    monkeypatch.setenv("Walmart_Authorization", "x")
    monkeypatch.setenv("WM_QOS_CORRELATION_ID", "y")
    paths.bat_log.write_text("Finished at Fri 10/23/2026 14:59:08.16 with exit code 1\n", encoding="utf-8")
    write_batch(paths, update=0, price=0, match=0, unmatched=0, lag_time=0)
    runner = FakeRunner({"zero_quantity": [(0, ok_rows(status=400), "")]})
    jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), runner)
    paths.pusher_log.write_text("store check Walmart: export 2026-10-23T18:00:49Z (1.0 h old), 3 at oversell risk\n", encoding="utf-8")
    rc = jobs.status(paths)
    text = paths.status.read_text()
    assert rc == 1 and "exited 1" in text and "zero_quantity" in text and "rejected" in text
    assert "store check Walmart" in text and text.rstrip().endswith("verdict: ATTENTION")


def test_status_ok_when_all_is_well(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    monkeypatch.setenv("Walmart_Authorization", "x")
    monkeypatch.setenv("WM_QOS_CORRELATION_ID", "y")
    paths.bat_log.write_text("Finished at Fri 10/23/2026 14:00:48.43 with exit code 0\n", encoding="utf-8")
    write_batch(paths)
    jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), FakeRunner())
    assert jobs.status(paths) == 0, paths.status.read_text()


def test_log_redacts_token_like_runs(tmp_path):
    paths = make_paths(tmp_path)
    jobs.Log(paths, "x", False).line("s", "failed", "token " + "A" * 40, ["Bearer " + "b" * 50])
    text = paths.jobs_log.read_text()
    assert "A" * 40 not in text and "b" * 50 not in text and "<redacted>" in text


def test_bat_log_ignores_other_computers(tmp_path):
    log = tmp_path / "bat.txt"
    log.write_text("Finished at Fri 10/23/2026 18:25:00.10 with exit code 0 on VM-Generator\n"
                   "Finished at Fri 10/23/2026 18:59:08.16 with exit code 1 on DESKTOP-M8PEG74\n", encoding="utf-8")
    when, code = jobs.last_export_run(log, host="VM-Generator")
    assert code == 0 and when.hour == 18 and when.minute == 25


def test_a_throttled_step_waits_before_its_next_attempt(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    write_batch(paths, zero_quantity=0, update=0, price=0, match=0, unmatched=0)
    runner = FakeRunner({"lag_time": [(0, ok_rows(status=429), ""), None]})
    log = jobs.Log(paths, "uploads", False)
    jobs.uploads(paths, False, log, runner)
    jobs.uploads(paths, False, log, runner)
    assert len(runner.calls) == 1, "no second attempt inside the throttle wait"
    later = jobs.now() + jobs._dt.timedelta(minutes=jobs.THROTTLE_WAIT_MIN + 1)
    monkeypatch.setattr(jobs, "now", lambda: later)
    jobs.uploads(paths, False, log, runner)
    assert len(runner.calls) == 2


def test_late_orders_are_counted_per_order(tmp_path):
    t = jobs._dt.datetime(2026, 10, 23, 18, 0)
    ms = lambda d: str(int(d.timestamp() * 1000))  # noqa: E731
    f = tmp_path / "orders.csv"
    f.write_text("purchaseOrderId,lineNumber,estimatedShipDate\n"
                 f"A,1,{ms(t - jobs._dt.timedelta(days=4, hours=1))}\nA,2,{ms(t - jobs._dt.timedelta(days=4, hours=1))}\n"
                 f"B,1,{ms(t - jobs._dt.timedelta(hours=2))}\nC,1,{ms(t + jobs._dt.timedelta(hours=5))}\n"
                 f"D,1,{ms(t + jobs._dt.timedelta(days=3))}\n", encoding="utf-8")
    got = jobs.late_orders(f, t)
    assert (got["pending"], got["late"], got["soon"]) == (4, 2, 1)
    assert got["first_cancel"] == "2026-10-23 17:00"


@pytest.mark.parametrize("code,expected_rc,outcome", [(0, 0, "done"), (2, 0, "skipped"), (1, 1, "failed")])
def test_export_runs_the_bat_worker_and_reads_its_exit_code(tmp_path, code, expected_rc, outcome):
    paths = make_paths(tmp_path)
    folder = tmp_path / "dir with space"
    folder.mkdir()
    bat = folder / "Inventory Refresh.bat"
    seen = folder / "seen.txt"
    bat.write_bytes(b'@echo off\r\nif /i "%~1"=="--worker" echo worker> "%~dp0seen.txt"\r\nexit /b ' + str(code).encode() + b"\r\n")
    assert jobs.export(paths, jobs.Log(paths, "export", False), bat=bat) == expected_rc
    assert seen.exists(), "the BAT ran its worker pass"
    assert f"export export {outcome}" in paths.jobs_log.read_text()


def healthy(paths, monkeypatch):
    monkeypatch.setenv("Walmart_Authorization", "x")
    monkeypatch.setenv("WM_QOS_CORRELATION_ID", "y")
    paths.bat_log.write_text("Finished at Fri 10/23/2026 14:00:48.43 with exit code 0\n", encoding="utf-8")
    write_batch(paths)
    jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), FakeRunner())


def write_late_order(paths):
    ms = int((jobs.now() - jobs._dt.timedelta(days=1)).timestamp() * 1000)
    paths.orders_csv.write_text(f"purchaseOrderId,lineNumber,estimatedShipDate\nA,1,{ms}\n", encoding="utf-8")


def test_status_exits_3_when_late_orders_are_the_only_attention(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    healthy(paths, monkeypatch)
    write_late_order(paths)
    assert jobs.status(paths) == jobs.STATUS_LATE_ONLY == 3
    assert paths.status.read_text().rstrip().endswith("verdict: ATTENTION (late orders only)")


def test_status_exits_1_when_late_orders_come_with_another_fault(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    healthy(paths, monkeypatch)
    write_late_order(paths)
    monkeypatch.delenv("WM_QOS_CORRELATION_ID")
    assert jobs.status(paths) == 1
    assert paths.status.read_text().rstrip().endswith("verdict: ATTENTION")
