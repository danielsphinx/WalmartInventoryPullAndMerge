"""State file safety for walmart_jobs.py (2026-11-10).

On 2026-11-04 19:37 three dry run jobs, each under its own job lock, wrote walmart_jobs_state.dry.json through
one shared temp name. The file was left with a second closing brace and every uploads, orders, tracking and
status run crashed for six days. These tests hold the repair: one state lock, a temp name per writer, a merge
that keeps other jobs' keys, a corrupt dry state that is set aside, a corrupt live state that is never reset,
a status file that is still written, and exit codes that tell a crash (4) and an unreachable Walmart (5) apart.

Run from the project folder:  "<compliance clone>\\.venv\\Scripts\\python.exe" -m pytest -q tests
"""
import json
import threading

import pytest

import walmart_jobs as jobs
from test_walmart_jobs import FakeRunner, make_paths, write_batch

CORRUPT = '{\n "runs": {}\n}\n}'  # the 2026-11-04 shape: one complete document, then a second closing brace


def test_a_save_keeps_keys_another_job_saved_meanwhile(tmp_path):
    path = make_paths(tmp_path).state(True)
    orders_run, tracking_run = jobs.load_state(path), jobs.load_state(path)
    orders_run["orders"]["last_ok"] = "2026-11-10T15:00:00"
    orders_run["runs"]["orders"] = "2026-11-10T15:00:00"
    tracking_run["tracking"]["last_outcome"] = "done"
    tracking_run["runs"]["tracking"] = "2026-11-10T15:00:01"
    jobs.save_state(path, orders_run)
    jobs.save_state(path, tracking_run)
    state = jobs.load_state(path)
    assert state["orders"]["last_ok"] == "2026-11-10T15:00:00", "the later save dropped the earlier job's keys"
    assert state["tracking"]["last_outcome"] == "done"
    assert state["runs"] == {"orders": "2026-11-10T15:00:00", "tracking": "2026-11-10T15:00:01"}


def test_a_save_keeps_a_feed_added_and_a_feed_removed_by_two_jobs(tmp_path):
    path = make_paths(tmp_path).state(False)
    start = jobs.load_state(path)
    start["feeds"] = {"OLD": {"step": "update", "batch": "b", "posted": "2026-11-10T14:00:00"}}
    jobs.save_state(path, start)
    uploads_run, feedstatus_run = jobs.load_state(path), jobs.load_state(path)
    uploads_run["feeds"]["NEW"] = {"step": "price", "batch": "b", "posted": "2026-11-10T15:00:00"}
    del feedstatus_run["feeds"]["OLD"]
    feedstatus_run["feed_results"].append({"feedId": "OLD", "feedStatus": "PROCESSED"})
    jobs.save_state(path, uploads_run)
    jobs.save_state(path, feedstatus_run)
    state = jobs.load_state(path)
    assert set(state["feeds"]) == {"NEW"}
    assert [r["feedId"] for r in state["feed_results"]] == ["OLD"]


def test_a_reference_taken_before_a_save_still_writes_after_it(tmp_path):
    """uploads() keeps `batch` and `record` across several saves: a save must not replace those objects."""
    path = make_paths(tmp_path).state(False)
    state = jobs.load_state(path)
    batch = state["batches"].setdefault("2026-11-10T15:00:00", {"steps": {}})
    record = batch["steps"].setdefault("update", {"outcome": None})
    jobs.save_state(path, state)
    other = jobs.load_state(path)
    other["runs"]["export"] = "2026-11-10T15:00:05"
    jobs.save_state(path, other)
    record["outcome"] = "done"
    jobs.save_state(path, state)
    final = jobs.load_state(path)
    assert final["batches"]["2026-11-10T15:00:00"]["steps"]["update"]["outcome"] == "done"
    assert final["runs"]["export"] == "2026-11-10T15:00:05"


def test_many_writers_at_once_leave_valid_json_and_every_key(tmp_path):
    path = make_paths(tmp_path).state(True)
    errors = []

    def writer(name):
        try:
            for i in range(40):
                state = jobs.load_state(path)
                state["runs"][name] = i
                jobs.save_state(path, state)
                json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001  (the test reports whatever a writer met)
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("uploads", "orders", "tracking", "status")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert jobs.load_state(path)["runs"] == {"uploads": 39, "orders": 39, "tracking": 39, "status": 39}
    assert [p.name for p in path.parent.glob("*.tmp")] == [], "a temp file was left behind"


def test_a_corrupt_dry_state_is_set_aside_and_the_run_continues(tmp_path):
    paths = make_paths(tmp_path)
    paths.state(True).write_text(CORRUPT, encoding="utf-8")
    log = jobs.Log(paths, "orders", True)
    runner = FakeRunner({"orders": [(0, [{"event": "end", "ok": True}], "DRY RUN orders: 3")]})
    assert jobs.orders(paths, True, log, runner) == 0
    aside = list(paths.logs.glob("walmart_jobs_state.dry.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == CORRUPT
    assert jobs.load_state(paths.state(True))["orders"]["last_ok"]
    assert "state_unreadable" in paths.jobs_log.read_text(encoding="utf-8")


def test_a_corrupt_live_state_is_never_reset_and_no_step_runs(tmp_path):
    paths = make_paths(tmp_path)
    write_batch(paths)
    paths.state(False).write_text(CORRUPT, encoding="utf-8")
    runner = FakeRunner()
    with pytest.raises(jobs.StateUnreadable):
        jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), runner)
    assert runner.calls == [], "a step ran on an unreadable live state: the batch would be sent again"
    assert paths.state(False).read_text(encoding="utf-8") == CORRUPT
    assert list(paths.logs.glob("*.corrupt-*")) == []


def test_main_returns_4_for_an_unreadable_live_state(tmp_path):
    paths = make_paths(tmp_path)
    paths.state(False).write_text(CORRUPT, encoding="utf-8")
    assert jobs.main(["feedstatus"], paths) == jobs.EXIT_CRASH == 4
    text = paths.jobs_log.read_text(encoding="utf-8")
    assert "[ATTENTION] live state file unreadable" in text


def test_main_returns_4_when_a_job_raises(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "status", boom)
    assert jobs.main(["status"], paths) == 4
    assert "status crashed | RuntimeError: boom" in paths.jobs_log.read_text(encoding="utf-8")


@pytest.mark.parametrize("which", [True, False])
def test_status_still_writes_its_file_when_a_state_file_is_unreadable(tmp_path, monkeypatch, which):
    paths = make_paths(tmp_path)
    monkeypatch.setenv("Walmart_Authorization", "x")
    monkeypatch.setenv("WM_QOS_CORRELATION_ID", "y")
    paths.bat_log.write_text("Finished at Fri 10/23/2026 14:00:48.43 with exit code 0\n", encoding="utf-8")
    paths.state(which).write_text(CORRUPT, encoding="utf-8")
    assert jobs.status(paths) == 1
    text = paths.status.read_text(encoding="utf-8")
    assert f"[ATTENTION] state file unreadable: {paths.state(which).name}" in text
    assert text.rstrip().endswith("verdict: ATTENTION")
    assert paths.state(which).read_text(encoding="utf-8") == CORRUPT, "status only reads; it never resets a state file"


def test_export_keeps_its_own_exit_code_when_the_live_state_is_unreadable(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    paths.state(False).write_text(CORRUPT, encoding="utf-8")
    monkeypatch.setattr(jobs.subprocess, "run", lambda *a, **k: type("Done", (), {"returncode": 0})())
    assert jobs.export(paths, jobs.Log(paths, "export", False), bat=tmp_path / "x.bat") == 0
    assert "state_unreadable" in paths.jobs_log.read_text(encoding="utf-8")


UNREACHABLE = ("RuntimeError: walmart_feed_http: token POST failed after 2 attempts (ConnectionError: Max retries exceeded "
               "(Caused by NameResolutionError: Failed to resolve: [Errno 11001] getaddrinfo failed))")


@pytest.mark.parametrize("error, expected", [(UNREACHABLE, 5), ("RuntimeError: HTTP 500: Internal Server Error", 1)])
def test_orders_exit_code_tells_unreachable_from_other_failures(tmp_path, error, expected):
    paths = make_paths(tmp_path)
    runner = FakeRunner({"orders": [(1, [{"event": "end", "ok": False, "error": error}], "")]})
    assert jobs.orders(paths, True, jobs.Log(paths, "orders", True), runner) == expected
    assert jobs.EXIT_UNREACHABLE == 5


@pytest.mark.parametrize("error, expected", [(UNREACHABLE, 5), ("RuntimeError: HTTP 400: bad row", 1)])
def test_tracking_exit_code_tells_unreachable_from_other_failures(tmp_path, error, expected):
    paths = make_paths(tmp_path)
    csv_path = tmp_path / "t.csv"
    csv_path.write_text("purchaseOrderId\n123\n", encoding="utf-8")
    runner = FakeRunner({"tracking": [(1, [{"event": "end", "ok": False, "error": error}], "")]})
    assert jobs.tracking(paths, True, jobs.Log(paths, "tracking", True), runner=runner, csv_path=csv_path) == expected


def test_feedstatus_exits_5_only_when_every_fault_is_unreachable(tmp_path):
    paths = make_paths(tmp_path)
    state = jobs.load_state(paths.state(False))
    state["feeds"] = {"A": {"step": "update", "batch": "b", "posted": jobs.iso(jobs.now())},
                      "B": {"step": "price", "batch": "b", "posted": jobs.iso(jobs.now())}}
    jobs.save_state(paths.state(False), state)

    def down(feed_id):
        raise ConnectionError("Max retries exceeded: getaddrinfo failed")

    assert jobs.feedstatus(paths, jobs.Log(paths, "feedstatus", False), fetch=down) == 5

    def mixed(feed_id):
        if feed_id == "A":
            raise ConnectionError("getaddrinfo failed")
        return {"feedStatus": "ERROR", "itemsReceived": 5, "itemsFailed": 5}

    assert jobs.feedstatus(paths, jobs.Log(paths, "feedstatus", False), fetch=mixed) == 1
