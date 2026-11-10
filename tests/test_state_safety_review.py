"""State file safety, second set: the cases the Sonnet review of 2026-11-10 found (see test_state_safety.py).

Run from the project folder:  "<compliance clone>\\.venv\\Scripts\\python.exe" -m pytest -q tests
"""
import json

import pytest

import walmart_jobs as jobs
from test_walmart_jobs import FakeRunner, make_paths, write_batch


def healthy_env(paths, monkeypatch):
    monkeypatch.setenv("Walmart_Authorization", "x")
    monkeypatch.setenv("WM_QOS_CORRELATION_ID", "y")
    paths.bat_log.write_text("Finished at Fri 10/23/2026 14:00:48.43 with exit code 0\n", encoding="utf-8")


@pytest.mark.parametrize("content", [b'{"runs": {}}\xff\xfe', b"", b"  \r\n", b"[1, 2]", b'{"runs": 5}'])
def test_every_kind_of_unreadable_dry_state_is_set_aside(tmp_path, content):
    paths = make_paths(tmp_path)
    paths.state(True).write_bytes(content)
    with pytest.raises(jobs.StateUnreadable):
        jobs.load_state(paths.state(True))
    state = jobs.load_state(paths.state(True), reset_corrupt=True, log=jobs.Log(paths, "orders", True))
    assert state["runs"] == {} and not paths.state(True).exists()
    aside = list(paths.logs.glob("walmart_jobs_state.dry.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_bytes() == content


def test_two_files_set_aside_in_one_second_are_both_kept(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    fixed = jobs.now()
    monkeypatch.setattr(jobs, "now", lambda: fixed)
    for content in ("AAA{", "BBB{"):
        paths.state(True).write_text(content, encoding="utf-8")
        jobs.load_state(paths.state(True), reset_corrupt=True)
    kept = sorted(p.read_text(encoding="utf-8") for p in paths.logs.glob("walmart_jobs_state.dry.json.corrupt-*"))
    assert kept == ["AAA{", "BBB{"]


def test_status_writes_its_file_when_the_state_lock_stays_busy(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    healthy_env(paths, monkeypatch)
    monkeypatch.setattr(jobs, "STATE_LOCK_WAIT_S", 0.2)
    with jobs.state_lock(paths.state(True), wait_s=5):
        assert jobs.status(paths) == 1
    text = paths.status.read_text(encoding="utf-8")
    assert "[ATTENTION] state file unreadable: walmart_jobs_state.dry.json: the state lock stayed busy" in text
    assert "dry state: unreadable" in text


def test_status_never_claims_shadow_mode_when_the_live_state_is_unreadable(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    healthy_env(paths, monkeypatch)
    dry = jobs.load_state(paths.state(True))
    dry["runs"]["uploads"] = jobs.iso(jobs.now())
    jobs.save_state(paths.state(True), dry)
    assert jobs.status(paths) in (0, 1) and "MODE: DRY RUN" in paths.status.read_text(encoding="utf-8")
    paths.state(False).write_text("{", encoding="utf-8")
    assert jobs.status(paths) == 1
    text = paths.status.read_text(encoding="utf-8")
    assert "MODE: DRY RUN" not in text and "uploads (live)" not in text and "feeds:" not in text
    assert "live state: unreadable" in text and "uploads (dry)" in text


def test_two_jobs_that_changed_different_steps_of_one_batch_both_keep_their_step(tmp_path):
    """seed marks step a while uploads, loaded earlier, finishes step b: neither mark is lost."""
    path = make_paths(tmp_path).state(False)
    start = jobs.load_state(path)
    start["batches"]["b1"] = {"steps": {"a": {"outcome": None, "attempts": 0}, "b": {"outcome": None, "attempts": 0}}}
    jobs.save_state(path, start)
    uploads_run, seed_run = jobs.load_state(path), jobs.load_state(path)
    seed_run["batches"]["b1"]["steps"]["a"]["outcome"] = "seeded"
    jobs.save_state(path, seed_run)
    uploads_run["batches"]["b1"]["steps"]["b"].update({"outcome": "done", "attempts": 1})
    jobs.save_state(path, uploads_run)
    steps = jobs.load_state(path)["batches"]["b1"]["steps"]
    assert steps == {"a": {"outcome": "seeded", "attempts": 0}, "b": {"outcome": "done", "attempts": 1}}


def test_a_step_record_held_across_a_save_that_took_another_jobs_change_still_writes(tmp_path):
    path = make_paths(tmp_path).state(False)
    start = jobs.load_state(path)
    start["batches"]["b1"] = {"steps": {"u": {"outcome": None}}}
    jobs.save_state(path, start)
    mine, other = jobs.load_state(path), jobs.load_state(path)
    record = mine["batches"]["b1"]["steps"]["u"]
    other["batches"]["b1"]["steps"]["u"]["seen"] = 1
    jobs.save_state(path, other)
    jobs.save_state(path, mine)  # no change of mine: takes the other job's change into the same objects
    assert record == {"outcome": None, "seen": 1}, "the held record was replaced, not updated"
    record["outcome"] = "done"
    jobs.save_state(path, mine)
    assert jobs.load_state(path)["batches"]["b1"]["steps"]["u"] == {"outcome": "done", "seen": 1}


def test_a_feed_result_equal_to_an_older_one_is_kept(tmp_path):
    path = make_paths(tmp_path).state(False)
    entry = {"feedId": "X", "feedStatus": "PROCESSED", "checked": "2026-11-10T15:00:00"}
    start = jobs.load_state(path)
    start["feed_results"].append(dict(entry))
    jobs.save_state(path, start)
    mine, other = jobs.load_state(path), jobs.load_state(path)
    other["feed_results"].append({"feedId": "Y", "feedStatus": "ERROR"})
    jobs.save_state(path, other)
    mine["feed_results"].append(dict(entry))
    mine["feed_results"].append(dict(entry))
    jobs.save_state(path, mine)
    assert [r["feedId"] for r in jobs.load_state(path)["feed_results"]] == ["X", "Y", "X", "X"]


def test_a_failed_save_can_be_repeated_without_doubling_anything(tmp_path, monkeypatch):
    path = make_paths(tmp_path).state(False)
    mine, other = jobs.load_state(path), jobs.load_state(path)
    other["feed_results"].append({"feedId": "R1"})
    other["runs"]["export"] = "2026-11-10T15:00:00"
    jobs.save_state(path, other)
    mine["feed_results"].append({"feedId": "R2"})
    real, calls = jobs._replace, []

    def fail_once(temp, target):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("in use")
        real(temp, target)

    monkeypatch.setattr(jobs, "_replace", fail_once)
    with pytest.raises(PermissionError):
        jobs.save_state(path, mine)
    assert mine["feed_results"] == [{"feedId": "R2"}] and "export" not in mine["runs"], "a failed save changed the job's state"
    jobs.save_state(path, mine)
    final = jobs.load_state(path)
    assert [r["feedId"] for r in final["feed_results"]] == ["R1", "R2"] and final["runs"]["export"] == "2026-11-10T15:00:00"
    assert list(path.parent.glob("*.tmp")) == []


def test_a_live_step_whose_result_cannot_be_saved_stops_all_later_uploads(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    healthy_env(paths, monkeypatch)
    write_batch(paths)
    log, runner = jobs.Log(paths, "uploads", False), FakeRunner()
    real_save, saves = jobs.save_state, []

    def save_fails_after_the_first_step(path, state):
        saves.append(1)
        if runner.calls:
            raise TimeoutError("walmart_jobs_state.json: the state lock stayed busy for 30 s")
        real_save(path, state)

    monkeypatch.setattr(jobs, "save_state", save_fails_after_the_first_step)
    monkeypatch.setattr(jobs, "SAVE_AFTER_SEND_S", 0)
    with pytest.raises(TimeoutError):
        jobs.uploads(paths, False, log, runner)
    assert [c[0] for c in runner.calls] == ["zero_quantity"], "a later step ran although the first result was not saved"
    unsaved = jobs.unsaved_files(paths)
    assert len(unsaved) == 1
    held = json.loads(unsaved[0].read_text(encoding="utf-8"))
    assert held["step"] == "zero_quantity" and held["record"]["feed_ids"] == ["F-zero_quantity"] and held["record"]["outcome"] == "done"
    monkeypatch.setattr(jobs, "save_state", real_save)
    assert jobs.uploads(paths, False, log, runner) == 1
    assert len(runner.calls) == 1, "uploads ran again while an unsaved result file exists: the step would be sent twice"
    assert "walmart_jobs_unsaved." in paths.jobs_log.read_text(encoding="utf-8")
    assert jobs.status(paths) == 1 and "walmart_jobs_unsaved." in paths.status.read_text(encoding="utf-8")
    assert jobs.uploads(paths, True, jobs.Log(paths, "uploads", True), FakeRunner()) == 0, "a dry run is not stopped by it"


def test_export_keeps_its_exit_code_when_the_state_lock_stays_busy(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    monkeypatch.setattr(jobs, "STATE_LOCK_WAIT_S", 0.2)
    monkeypatch.setattr(jobs.subprocess, "run", lambda *a, **k: type("Done", (), {"returncode": 0})())
    with jobs.state_lock(paths.state(False), wait_s=5):
        assert jobs.export(paths, jobs.Log(paths, "export", False), bat=tmp_path / "x.bat") == 0
    assert "state_unreadable" in paths.jobs_log.read_text(encoding="utf-8")


@pytest.mark.parametrize("problem, expected", [
    ("ConnectionError: HTTPSConnectionPool: Max retries exceeded (Caused by NameResolutionError: getaddrinfo failed)", True),
    ("ConnectTimeout: HTTPSConnectionPool(host='marketplace.walmartapis.com', port=443): timed out", True),
    ("ReadTimeout: HTTPSConnectionPool(host='marketplace.walmartapis.com', port=443): Read timed out. (read timeout=60)", True),
    ("ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))", True),
    ("RetryError: Max retries exceeded (Caused by ResponseError('too many 503 error responses'))", False),
    ("ConnectionError: Max retries exceeded (Caused by ResponseError('too many 503 error responses'))", False),
    ("SSLError: Max retries exceeded (Caused by SSLError(SSLCertVerificationError('certificate verify failed')))", False),
    ("RuntimeError: HTTP 500: Internal Server Error", False),
    ("RuntimeError: HTTP 401: Unauthorized", False),
    ("exit 1", False),
])
def test_unreachable_means_no_answer_from_walmart_and_nothing_else(problem, expected):
    assert jobs.unreachable(problem) is expected


def test_a_crash_line_carries_the_end_of_the_traceback(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "status", boom)
    assert jobs.main(["status"], paths) == 4
    text = paths.jobs_log.read_text(encoding="utf-8")
    assert "    | " in text and 'raise RuntimeError("boom")' in text


def test_status_with_an_unreadable_state_and_late_orders_is_exit_1_not_3(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    healthy_env(paths, monkeypatch)
    ms = int((jobs.now() - jobs._dt.timedelta(days=1)).timestamp() * 1000)
    paths.orders_csv.write_text(f"purchaseOrderId,lineNumber,estimatedShipDate\nA,1,{ms}\n", encoding="utf-8")
    paths.state(True).write_text("{", encoding="utf-8")
    assert jobs.status(paths) == 1
    assert paths.status.read_text(encoding="utf-8").rstrip().endswith("verdict: ATTENTION")


def test_a_dry_save_that_meets_an_unreadable_file_says_what_happens_next(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)

    def orders_then_corrupt(paths_, dry, log, runner=None):
        state = jobs.load_state(paths_.state(True), reset_corrupt=True, log=log)
        paths_.state(True).write_text("{", encoding="utf-8")
        jobs.save_state(paths_.state(True), state)
        return 0

    monkeypatch.setattr(jobs, "orders", orders_then_corrupt)
    assert jobs.main(["orders", "--dry-run"], paths) == 4
    text = paths.jobs_log.read_text(encoding="utf-8")
    assert "[ATTENTION] dry state file unreadable" in text and "the next dry run sets the file aside" in text
