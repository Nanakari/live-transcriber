import socket
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.cli_args import build_parser
from app.web import routes
from app.web.jobs import Job
from app.web.server import create_app


def test_web_transcribe_and_preview_passes_existing_subtitle_to_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    commands = []
    previews = []
    subtitle = tmp_path / "existing.srt"
    source = tmp_path / "source.wav"
    def start(module, command, **kwargs):
        commands.append(command)
        return Job(job_id="test", module=module, command=command)
    monkeypatch.setattr(routes.jobs, "start", start)
    monkeypatch.setattr(cli, "transcribe_task", lambda args: {
        "transcript": tmp_path / "transcript.json", "audio": source,
    })
    monkeypatch.setattr(cli, "_create_preview", lambda args: previews.append(args) or {})
    monkeypatch.setattr(cli, "_print_preview_result", lambda result: None)
    with TestClient(create_app()) as client:
        response = client.post("/api/pipeline", json={
            "module_transcribe": True, "module_analyze": False, "module_preview": True,
            "input": str(source), "subtitle": str(subtitle),
        })
    assert response.status_code == 200
    args = build_parser().parse_args(commands[0][2:])
    assert cli.handle_pipeline(args) == 0
    assert len(previews) == 1
    assert previews[0].subtitle == str(subtitle)
    assert previews[0].audio == str(source)


def test_web_rejects_missing_subtitle_before_starting_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    def unexpected_start(*args, **kwargs):
        pytest.fail("invalid pipeline started a job")
    monkeypatch.setattr(routes.jobs, "start", unexpected_start)
    with TestClient(create_app()) as client:
        response = client.post("/api/pipeline", json={
            "module_transcribe": True, "module_analyze": False, "module_preview": True,
            "input": "source.wav",
        })
    assert response.status_code == 400


def test_ipv6_port_check_and_web_browser_url(monkeypatch):
    try:
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        listener.bind(("::1", 0))
        listener.listen()
    except OSError:
        pytest.skip("IPv6 loopback is unavailable on this machine")
    port = listener.getsockname()[1]
    try:
        assert not routes.is_port_available("::1", port)
    finally:
        listener.close()
    assert routes.is_port_available("::1", port)
    opened = []
    served = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: served.append(kwargs))
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(cli.threading, "Timer", lambda delay, callback: SimpleNamespace(start=callback))
    args = build_parser().parse_args(["web", "--host", "::1", "--port", str(port), "--open-browser"])
    assert cli.handle_web(args) == 0
    assert opened == [f"http://[::1]:{port}"]
    assert served[0]["host"] == "::1"
    assert served[0]["port"] == port


def test_ipv6_socket_creation_failure_is_unavailable(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("IPv6 disabled")
    monkeypatch.setattr(routes.socket, "socket", unavailable)
    assert not routes.is_port_available("::1", 7860)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1:7860", "[::1]", "[::1]:7860"])
def test_allowed_local_host_can_load_page(host):
    with TestClient(create_app()) as client:
        response = client.get("/", headers={"Host": host, "Origin": f"http://{host}"})
    assert response.status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "[::1].evil.example",
                                 "[::1]@evil.example", "evil@localhost", "[::2]:7860", "localhost:abc"])
def test_invalid_hosts_remain_rejected(host):
    with TestClient(create_app()) as client:
        assert client.get("/", headers={"Host": host}).status_code == 400


def test_ipv6_page_does_not_allow_cross_origin_requests():
    with TestClient(create_app()) as client:
        response = client.post("/api/lifecycle/shutdown", headers={
            "Host": "[::1]:7860", "Origin": "http://[::1]:9999",
        })
    assert response.status_code == 403
