"""Unit tests for message transport helpers and WebSocket demultiplexing.

[INPUT]: message_transport module, message_daemon, WsClient, fake websocket connection
[OUTPUT]: Regression coverage for receive-mode persistence, localhost daemon
          proxying, and single-reader response/notification demultiplexing
[POS]: Transport selection and WebSocket client safety tests

[PROTOCOL]:
1. Update this header when logic changes
2. Check the containing folder's CLAUDE.md after updates
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_scripts_dir))

import message_daemon  # noqa: E402
import message_transport  # noqa: E402
from utils.config import SDKConfig  # noqa: E402
from utils.ws import WsClient  # noqa: E402


class FakeConnection:
    """Minimal websocket-like connection for WsClient unit tests."""

    def __init__(self) -> None:
        self.sent_payloads: list[str] = []
        self.recv_queue: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent_payloads.append(payload)

    async def recv(self) -> str:
        return await self.recv_queue.get()

    def ping(self):
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        waiter.set_result(None)
        return waiter

    async def close(self) -> None:
        self.closed = True


def _build_config(tmp_path: Path) -> SDKConfig:
    """Build a config object rooted under a temporary data dir."""
    return SDKConfig(
        user_service_url="https://example.com",
        molt_message_url="https://example.com",
        did_domain="example.com",
        credentials_dir=tmp_path / "credentials",
        data_dir=tmp_path / "data",
    )


def test_write_and_load_receive_mode(tmp_path: Path) -> None:
    """Transport mode should round-trip through settings.json."""
    config = _build_config(tmp_path)

    settings_path = message_transport.write_receive_mode(
        message_transport.RECEIVE_MODE_WEBSOCKET,
        config=config,
    )

    assert settings_path.exists()
    assert (
        message_transport.load_receive_mode(config)
        == message_transport.RECEIVE_MODE_WEBSOCKET
    )


def test_ws_client_demultiplexes_response_and_notification() -> None:
    """A notification interleaved with a response should not be lost."""

    async def _run() -> None:
        config = SDKConfig(
            user_service_url="https://example.com",
            molt_message_url="https://example.com",
            did_domain="example.com",
        )
        identity = SimpleNamespace(jwt_token="token")
        ws = WsClient(config, identity)
        fake_conn = FakeConnection()
        ws._conn = fake_conn
        ws._reader_task = asyncio.create_task(ws._reader_loop())

        rpc_task = asyncio.create_task(ws.send_rpc("send", {"content": "hello"}))
        await asyncio.sleep(0)

        await fake_conn.recv_queue.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "new_message",
                    "params": {"id": "msg-1", "content": "incoming"},
                }
            )
        )
        await fake_conn.recv_queue.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"id": "out-1", "server_seq": 7},
                }
            )
        )

        result = await rpc_task
        notification = await ws.receive_notification(timeout=0.1)

        assert result == {"id": "out-1", "server_seq": 7}
        assert notification is not None
        assert notification["method"] == "new_message"
        assert json.loads(fake_conn.sent_payloads[0])["method"] == "send"

        if ws._reader_task is not None:
            ws._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await ws._reader_task

    asyncio.run(_run())


def test_websocket_transport_uses_local_daemon(tmp_path: Path) -> None:
    """WebSocket mode message RPC should go through the localhost daemon."""

    async def _run() -> None:
        config = _build_config(tmp_path)
        token = "daemon-token"
        message_transport.write_receive_mode(
            message_transport.RECEIVE_MODE_WEBSOCKET,
            config=config,
            extra_transport_fields={
                "local_daemon_host": "127.0.0.1",
                "local_daemon_port": 18881,
                "local_daemon_token": token,
            },
        )

        captured: dict[str, object] = {}

        async def _handler(method: str, params: dict[str, object]) -> dict[str, object]:
            captured["method"] = method
            captured["params"] = params
            return {"id": "msg-1", "server_seq": 9}

        daemon = message_daemon.LocalMessageDaemon(
            message_daemon.LocalDaemonSettings(
                host="127.0.0.1",
                port=18881,
                token=token,
            ),
            _handler,
        )
        await daemon.start()
        try:
            result = await message_transport.websocket_message_rpc_call(
                "send",
                {"content": "hello"},
                config=config,
            )
        finally:
            await daemon.close()

        assert result == {"id": "msg-1", "server_seq": 9}
        assert captured["method"] == "send"
        assert captured["params"] == {"content": "hello"}

    asyncio.run(_run())
