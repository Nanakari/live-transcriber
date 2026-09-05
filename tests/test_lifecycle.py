from __future__ import annotations

from app.web import lifecycle as lifecycle_module
from app.web.lifecycle import BrowserLifecycle


def test_heartbeat_keeps_client_without_scheduling_shutdown(monkeypatch) -> None:
    lifecycle = BrowserLifecycle()
    scheduled = []
    monkeypatch.setattr(lifecycle, "schedule_shutdown_if_idle", lambda: scheduled.append(True))

    status = lifecycle.heartbeat("browser-1")

    assert status["active_clients"] == 1
    assert status["shutdown_scheduled"] is False
    assert scheduled == []


def test_status_does_not_expire_idle_browser_client() -> None:
    lifecycle = BrowserLifecycle()
    lifecycle.heartbeat("browser-1")

    assert lifecycle.status()["active_clients"] == 1
    assert lifecycle.status()["shutdown_scheduled"] is False


def test_closing_webpage_requests_shutdown(monkeypatch) -> None:
    lifecycle = BrowserLifecycle()
    lifecycle.heartbeat("browser-1")
    scheduled = []
    monkeypatch.setattr(lifecycle, "schedule_shutdown_if_idle", lambda: scheduled.append(True))

    status = lifecycle.close("browser-1")

    assert status["active_clients"] == 0
    assert scheduled == [True]


def test_close_waits_for_running_job_before_exit(monkeypatch) -> None:
    lifecycle = BrowserLifecycle()
    scheduled = []
    exited = []
    monkeypatch.setattr(lifecycle_module.jobs, "has_running_jobs", lambda: True)
    monkeypatch.setattr(lifecycle, "schedule_shutdown_if_idle", lambda: scheduled.append(True))
    monkeypatch.setattr(lifecycle_module.os, "_exit", lambda code: exited.append(code))

    lifecycle._shutdown_if_still_idle()

    assert scheduled == [True]
    assert exited == []


def test_close_exits_after_jobs_finish(monkeypatch) -> None:
    lifecycle = BrowserLifecycle()
    exited = []
    monkeypatch.setattr(lifecycle_module.jobs, "has_running_jobs", lambda: False)
    monkeypatch.setattr(lifecycle_module.os, "_exit", lambda code: exited.append(code))

    lifecycle._shutdown_if_still_idle()

    assert exited == [0]
