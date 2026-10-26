import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
from conftest import FakeProvider

from document_extraction_api.app import create_app


def test_python_client_over_real_http(settings, png, tmp_path):
    app = create_app(settings, FakeProvider)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        source = tmp_path / "synthetic.png"
        source.write_bytes(png)
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parents[1] / "examples" / "extract.py"),
                str(source),
                "--url",
                f"http://127.0.0.1:{port}",
                "--idempotency-key",
                "real-http-001",
            ],
            env={**os.environ, "DEX_CLIENT_KEY": "a" * 32},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["status"] == "succeeded"
        assert result["data"]["document_number"] == "TEST-1"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    assert not thread.is_alive()
