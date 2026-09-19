from types import SimpleNamespace

import pytest

from app.web import jobs as module


@pytest.mark.parametrize("during_output", [False, True])
def test_log_failure_finishes_job_and_releases_slot(monkeypatch, tmp_path, during_output):
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    manager = module.JobManager()
    job = module.Job(job_id="broken", module="pipeline", command=["test"], env_overrides={"KEY": "dummy"})
    manager._jobs[job.job_id] = job
    manager._heavy_running = job.job_id
    process = SimpleNamespace(pid=123, stdout=iter(["output\n"]))
    terminated = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(module, "terminate_process_tree", terminated.append)

    def write_log(job, text):
        if not during_output or not text.startswith("COMMAND:"):
            raise OSError("disk full")

    monkeypatch.setattr(manager, "_append_log", write_log)
    monkeypatch.setattr(module, "guess_output_dir", lambda *a: (_ for _ in ()).throw(OSError("unavailable")))
    manager._run_job(job)
    assert job.status == "failed"
    assert job.error == "disk full"
    assert job.finished_at is not None
    assert job.env_overrides == {}
    assert not manager.has_running_jobs()
    assert manager._heavy_running is None
    assert terminated == ([process] if during_output else [])
    # A subsequent heavy task is accepted rather than returning HTTP 409.
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: None)
    assert manager.start("pipeline", ["next"]).status == "pending"
