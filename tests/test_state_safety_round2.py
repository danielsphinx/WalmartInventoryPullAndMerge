"""State file safety, third set: the cases the second Sonnet review of 2026-11-10 found.

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


def test_a_live_step_is_recorded_even_when_its_log_line_cannot_be_written(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    write_batch(paths)
    log, runner = jobs.Log(paths, "uploads", False), FakeRunner()
    real_line = jobs.Log.line

    def line(self, step, outcome, *args, **kwargs):
        if outcome == "done":
            raise PermissionError("walmart_jobs.log is in use")
        return real_line(self, step, outcome, *args, **kwargs)

    monkeypatch.setattr(jobs.Log, "line", line)
    with pytest.raises(PermissionError):
        jobs.uploads(paths, False, log, runner)
    step = jobs.load_state(paths.state(False))["batches"]["2026-10-23T15:40:47"]["steps"]["zero_quantity"]
    assert step["outcome"] == "done" and step["feed_ids"] == ["F-zero_quantity"], "the step ran and the state does not say so"
    monkeypatch.setattr(jobs.Log, "line", real_line)
    jobs.uploads(paths, False, log, runner)
    assert [c[0] for c in runner.calls].count("zero_quantity") == 1, "the step was sent a second time"


def test_any_error_from_the_save_after_a_live_step_ends_in_an_unsaved_file(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    write_batch(paths)
    runner, real_save = FakeRunner(), jobs.save_state

    def save(path, state):
        if runner.calls:
            raise ValueError("not a save error anyone expected")
        real_save(path, state)

    monkeypatch.setattr(jobs, "save_state", save)
    monkeypatch.setattr(jobs, "SAVE_AFTER_SEND_S", 0)
    with pytest.raises(ValueError):
        jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), runner)
    files = jobs.unsaved_files(paths)
    assert len(files) == 1 and files[0].parent == paths.logs
    assert json.loads(files[0].read_text(encoding="utf-8"))["record"]["feed_ids"] == ["F-zero_quantity"]


def test_the_unsaved_file_goes_to_this_pc_when_the_log_folder_cannot_be_written(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    write_batch(paths)
    runner, real_save, real_write = FakeRunner(), jobs.save_state, jobs.Path.write_text

    def save(path, state):
        if runner.calls:
            raise OSError("the share is gone")
        real_save(path, state)

    def write_text(self, *args, **kwargs):
        if runner.calls and self.parent == paths.logs:
            raise FileNotFoundError("the share is gone")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(jobs, "save_state", save)
    monkeypatch.setattr(jobs, "SAVE_AFTER_SEND_S", 0)
    monkeypatch.setattr(jobs.Path, "write_text", write_text)
    with pytest.raises(OSError, match="the share is gone"):
        jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), runner)
    monkeypatch.setattr(jobs.Path, "write_text", real_write)
    monkeypatch.setattr(jobs, "save_state", real_save)
    files = jobs.unsaved_files(paths)
    assert len(files) == 1 and files[0].parent == paths.local
    assert json.loads(files[0].read_text(encoding="utf-8"))["step"] == "zero_quantity"
    assert jobs.uploads(paths, False, jobs.Log(paths, "uploads", False), runner) == 1 and len(runner.calls) == 1


@pytest.mark.parametrize("spoil", ["orders_bytes", "orders_nul", "orders_huge_date", "marker_bytes"])
def test_status_writes_its_file_whatever_the_files_it_reads_hold(tmp_path, monkeypatch, spoil):
    paths = make_paths(tmp_path)
    healthy_env(paths, monkeypatch)
    header = b"purchaseOrderId,lineNumber,estimatedShipDate\n"
    if spoil == "orders_bytes":
        paths.orders_csv.write_bytes(header + b"A\xff\xfe,1,1\n")
    elif spoil == "orders_nul":
        paths.orders_csv.write_bytes(header + b"A\x00,1,1\n")
    elif spoil == "orders_huge_date":
        paths.orders_csv.write_bytes(header + b"A,1,99999999999999999999999\n")
    else:
        paths.marker.write_bytes(b"run_time=\xff\xfe2026\n")
    rc = jobs.status(paths)
    text = paths.status.read_text(encoding="utf-8")
    assert text.startswith("walmart jobs status written ") and text.rstrip().split("\n")[-1].startswith("verdict: ")
    if spoil == "orders_huge_date":
        assert "orders past Expected Ship Date: 0 of 0 pending" in text
    else:
        assert rc == 1 and "could not be read" in text
    assert "ShipStation pusher:" in text, "the checks after the unreadable file were skipped"


def test_status_writes_its_file_when_a_check_raises_something_unforeseen(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    healthy_env(paths, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("unforeseen")

    monkeypatch.setattr(jobs, "last_export_run", boom)
    assert jobs.status(paths) == 1
    text = paths.status.read_text(encoding="utf-8")
    assert "[ATTENTION] status could not finish its checks (RuntimeError: unforeseen)" in text
    assert text.rstrip().endswith("verdict: ATTENTION")


def test_a_top_level_key_this_version_does_not_know_survives_a_save(tmp_path):
    path = make_paths(tmp_path).state(False)
    path.write_text(json.dumps({"runs": {}, "future": {"a": 1}, "note": "x"}), encoding="utf-8")
    mine, other = jobs.load_state(path), jobs.load_state(path)
    other["future"]["b"] = 2
    other["note"] = "y"
    other["added_later"] = {"k": 1}
    jobs.save_state(path, other)
    mine["runs"]["orders"] = "2026-11-10T16:00:00"
    jobs.save_state(path, mine)
    final = jobs.load_state(path)
    assert final["future"] == {"a": 1, "b": 2} and final["note"] == "y" and final["added_later"] == {"k": 1}
    assert final["runs"] == {"orders": "2026-11-10T16:00:00"} and final["feed_results"] == []


def test_a_state_file_that_vanished_is_written_whole_not_emptied(tmp_path):
    path = make_paths(tmp_path).state(False)
    start = jobs.load_state(path)
    start["batches"]["b1"] = {"steps": {"a": {"outcome": "done"}}}
    start["feeds"]["F1"] = {"step": "a", "batch": "b1", "posted": "2026-11-10T15:00:00"}
    jobs.save_state(path, start)
    mine = jobs.load_state(path)
    path.unlink()
    mine["runs"]["feedstatus"] = "2026-11-10T16:00:00"
    jobs.save_state(path, mine)
    final = jobs.load_state(path)
    assert final["batches"]["b1"]["steps"]["a"]["outcome"] == "done" and "F1" in final["feeds"]


@pytest.mark.parametrize("job", ["status", "feedstatus"])
def test_main_returns_4_even_when_the_log_cannot_be_written(tmp_path, monkeypatch, job):
    paths = make_paths(tmp_path)
    if job == "feedstatus":
        paths.state(False).write_text("{", encoding="utf-8")
    else:
        monkeypatch.setattr(jobs, "status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    def no_log(self, *args, **kwargs):
        raise OSError("the share is gone")

    monkeypatch.setattr(jobs.Log, "line", no_log)
    assert jobs.main([job], paths) == 4


@pytest.mark.parametrize("problem, expected", [
    ("ConnectionResetError: [WinError 10054] An existing connection was forcibly closed by the remote host", True),
    ("ConnectionRefusedError: [WinError 10061] No connection could be made because the target machine actively refused it", True),
    ("ChunkedEncodingError: ('Connection broken: IncompleteRead(0 bytes read)', IncompleteRead(0 bytes read))", True),
    ("SSLError: HTTPSConnectionPool: Max retries exceeded (Caused by SSLError(SSLEOFError(8, 'EOF occurred in violation of protocol')))", True),
    ("SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", False),
    ("RuntimeError: walmart_feed_http: token POST failed after 2 attempts (ConnectionError: Max retries exceeded "
     "(Caused by NameResolutionError: Failed to resolve: [Errno 11001] getaddrinfo failed))", True),
    ("RuntimeError: HTTP 429: Too Many Requests", False),
])
def test_unreachable_second_set(problem, expected):
    assert jobs.unreachable(problem) is expected
