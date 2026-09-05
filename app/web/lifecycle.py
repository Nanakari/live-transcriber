from __future__ import annotations

import os
import threading

from .jobs import jobs


class BrowserLifecycle:
    def __init__(self, *, shutdown_delay_seconds: float = 3.0) -> None:
        self.shutdown_delay_seconds = shutdown_delay_seconds
        self._clients: set[str] = set()
        self._lock = threading.Lock()
        self._shutdown_scheduled = False

    def heartbeat(self, client_id: str) -> dict[str, object]:
        if not client_id:
            return self.status()
        with self._lock:
            self._clients.add(client_id)
        return self.status()

    def close(self, client_id: str) -> dict[str, object]:
        with self._lock:
            if client_id:
                self._clients.discard(client_id)
        self.schedule_shutdown_if_idle()
        return self.status()

    def status(self) -> dict[str, object]:
        with self._lock:
            active_clients = len(self._clients)
        return {
            "active_clients": active_clients,
            "running_jobs": jobs.has_running_jobs(),
            "shutdown_scheduled": self._shutdown_scheduled,
        }

    def schedule_shutdown_if_idle(self) -> None:
        with self._lock:
            if self._shutdown_scheduled:
                return
            self._shutdown_scheduled = True
        timer = threading.Timer(self.shutdown_delay_seconds, self._shutdown_if_still_idle)
        timer.daemon = True
        timer.start()

    def shutdown_now(self) -> dict[str, object]:
        jobs.stop_all()
        with self._lock:
            self._shutdown_scheduled = True
            self._clients.clear()
        timer = threading.Timer(0.4, lambda: os._exit(0))
        timer.daemon = True
        timer.start()
        return {"active_clients": 0, "running_jobs": False, "shutdown_scheduled": True}

    def _shutdown_if_still_idle(self) -> None:
        should_exit = False
        should_retry = False
        with self._lock:
            if self._clients:
                self._shutdown_scheduled = False
            elif jobs.has_running_jobs():
                self._shutdown_scheduled = False
                should_retry = True
            else:
                should_exit = True
        if should_exit:
            os._exit(0)
        if should_retry:
            self.schedule_shutdown_if_idle()


lifecycle = BrowserLifecycle()
