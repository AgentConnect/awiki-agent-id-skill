"""Regression tests for ws_listener credential routing and reconnect catch-up.

[INPUT]: ws_listener helpers with monkeypatched auth, RPC, routing, and storage
[OUTPUT]: Coverage for secondary-credential daemon sends, paginated catch-up,
          and normalized offline message persistence
[POS]: WebSocket listener unit tests for daemon proxying and reconnect recovery

[PROTOCOL]:
1. Update this header when logic changes
2. Check the containing folder's CLAUDE.md after updates
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

import ws_listener  # noqa: E402
from utils.config import SDKConfig  # noqa: E402


class _FakeWsClient:
    """Small WsClient stub that records outbound RPC calls."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = list(responses or [])

    async def send_rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "mark_read":
            return {"updated_count": len(params.get("message_ids", []))}
        if not self._responses:
            raise AssertionError(f"Unexpected RPC call: {method}")
        return self._responses.pop(0)


def _build_config(tmp_path: Path) -> SDKConfig:
    """Build a config object rooted under a temporary data dir."""
    return SDKConfig(
        user_service_url="https://example.com",
        molt_message_url="https://example.com",
        did_domain="example.com",
        credentials_dir=tmp_path / "credentials",
        data_dir=tmp_path / "data",
    )


def test_active_ws_rpc_proxy_routes_calls_by_credential(
    tmp_path: Path,
) -> None:
    """Each credential should use its own active WsClient inside one process."""
    fake_default = _FakeWsClient(responses=[{"id": "default-msg"}])
    fake_sender = _FakeWsClient(responses=[{"id": "sender-msg"}])
    proxy = ws_listener._ActiveWsRpcProxy(config=_build_config(tmp_path))
    proxy.set_client("default", fake_default)
    proxy.set_client("sender", fake_sender)

    result = asyncio.run(
        proxy.call(
            "send",
            {"sender_did": "did:sender", "receiver_did": "did:peer", "content": "hello"},
            "sender",
        )
    )

    assert result == {"id": "sender-msg"}
    assert fake_default.calls == []
    assert fake_sender.calls == [
        (
            "send",
            {
                "sender_did": "did:sender",
                "receiver_did": "did:peer",
                "content": "hello",
            },
        )
    ]


def test_credential_ws_supervisor_starts_secondary_session_on_demand(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The single process should lazily start one listen loop per credential."""
    started: list[str] = []

    async def _fake_listen_loop(
        credential_name: str,
        cfg: object,
        config: SDKConfig | None = None,
        rpc_proxy: ws_listener._ActiveWsRpcProxy | None = None,
    ) -> None:
        del cfg, config
        started.append(credential_name)
        assert rpc_proxy is not None
        rpc_proxy.set_client(
            credential_name,
            _FakeWsClient(responses=[{"id": f"{credential_name}-msg"}]),
        )
        await asyncio.Event().wait()

    monkeypatch.setattr(ws_listener, "listen_loop", _fake_listen_loop)

    async def _run() -> None:
        supervisor = ws_listener._CredentialWsSupervisor(
            cfg=object(),
            config=_build_config(tmp_path),
        )
        try:
            result = await supervisor.call(
                "send",
                {"content": "hello"},
                "sender",
            )
            assert result == {"id": "sender-msg"}
            assert started == ["sender"]
        finally:
            await supervisor.close()

    asyncio.run(_run())


def test_credential_ws_supervisor_starts_all_known_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup should create one listener task per known credential."""
    started: list[str] = []

    async def _fake_listen_loop(
        credential_name: str,
        cfg: object,
        config: SDKConfig | None = None,
        rpc_proxy: ws_listener._ActiveWsRpcProxy | None = None,
    ) -> None:
        del cfg, config
        started.append(credential_name)
        assert rpc_proxy is not None
        rpc_proxy.set_client(
            credential_name,
            _FakeWsClient(responses=[{"id": f"{credential_name}-msg"}]),
        )
        await asyncio.Event().wait()

    monkeypatch.setattr(ws_listener, "listen_loop", _fake_listen_loop)

    async def _run() -> None:
        supervisor = ws_listener._CredentialWsSupervisor(
            cfg=object(),
            config=_build_config(tmp_path),
        )
        try:
            await supervisor.ensure_all_started(["default", "sender"])
            await asyncio.sleep(0)
            assert started == ["default", "sender"]
        finally:
            await supervisor.close()

    asyncio.run(_run())


def test_catch_up_inbox_paginates_before_advancing_cursor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reconnect catch-up should read all pages before persisting the sync cursor."""
    config = _build_config(tmp_path)
    fake_ws = _FakeWsClient(
        responses=[
            {
                "messages": [
                    {"id": "msg-3", "type": "text", "sender_did": "did:bob", "content": "c", "created_at": "2026-03-20T10:03:00+00:00"},
                    {"id": "msg-2", "type": "text", "sender_did": "did:bob", "content": "b", "created_at": "2026-03-20T10:02:00+00:00"},
                ],
                "has_more": True,
            },
            {
                "messages": [
                    {"id": "msg-1", "type": "text", "sender_did": "did:bob", "content": "a", "created_at": "2026-03-20T10:01:00+00:00"},
                ],
                "has_more": False,
            },
        ]
    )
    saved_cursor: list[str] = []
    stored_payloads: list[Any] = []
    marked_locally: list[str] = []

    monkeypatch.setattr(ws_listener, "_load_inbox_sync_since", lambda *args, **kwargs: "2026-03-20T09:00:00+00:00")
    monkeypatch.setattr(ws_listener, "_save_inbox_sync_since", lambda credential_name, since, config=None: saved_cursor.append(since))
    monkeypatch.setattr(ws_listener.local_store, "get_message_by_id", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ws_listener,
        "create_authenticator",
        lambda credential_name, config: (object(), {"did": "did:alice"}),
    )

    async def _fake_auto_process(
        messages: list[dict[str, Any]],
        *,
        local_did: str,
        auth: Any,
        credential_name: str,
    ) -> tuple[list[dict[str, Any]], list[str], object]:
        del local_did, auth, credential_name
        return messages, [], object()

    async def _fake_forward(*args, **kwargs) -> None:
        del args, kwargs

    monkeypatch.setattr(ws_listener, "_auto_process_e2ee_messages", _fake_auto_process)
    monkeypatch.setattr(ws_listener, "_store_inbox_messages", lambda credential_name, my_did, inbox: stored_payloads.append(inbox))
    monkeypatch.setattr(
        ws_listener,
        "_mark_local_messages_read",
        lambda *, credential_name, owner_did, message_ids: marked_locally.extend(message_ids),
    )
    monkeypatch.setattr(ws_listener, "classify_message", lambda params, my_did, cfg: None)
    monkeypatch.setattr(ws_listener, "_forward", _fake_forward)

    asyncio.run(
        ws_listener._catch_up_inbox(
            credential_name="default",
            my_did="did:alice",
            cfg=object(),
            config=config,
            ws=fake_ws,
            http=object(),
            local_db=object(),
            channels=[],
        )
    )

    get_inbox_calls = [
        params
        for method, params in fake_ws.calls
        if method == "get_inbox"
    ]
    assert get_inbox_calls == [
        {"user_did": "did:alice", "limit": 50, "since": "2026-03-20T09:00:00+00:00"},
        {"user_did": "did:alice", "limit": 50, "since": "2026-03-20T09:00:00+00:00", "skip": 2},
    ]
    assert saved_cursor == ["2026-03-20T10:03:00+00:00"]
    assert stored_payloads == [[
        {"id": "msg-1", "type": "text", "sender_did": "did:bob", "content": "a", "created_at": "2026-03-20T10:01:00+00:00"},
        {"id": "msg-2", "type": "text", "sender_did": "did:bob", "content": "b", "created_at": "2026-03-20T10:02:00+00:00"},
        {"id": "msg-3", "type": "text", "sender_did": "did:bob", "content": "c", "created_at": "2026-03-20T10:03:00+00:00"},
    ]]
    assert sorted(marked_locally) == ["msg-1", "msg-2", "msg-3"]


def test_catch_up_inbox_persists_normalized_e2ee_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Offline E2EE catch-up should cache decrypted user-visible payloads, not ciphertext."""
    config = _build_config(tmp_path)
    fake_ws = _FakeWsClient(
        responses=[
            {
                "messages": [
                    {
                        "id": "cipher-1",
                        "type": "e2ee_msg",
                        "sender_did": "did:bob",
                        "content": "{\"ciphertext\":\"abc\"}",
                        "created_at": "2026-03-20T10:01:00+00:00",
                    }
                ],
                "has_more": False,
            }
        ]
    )
    stored_payloads: list[Any] = []

    monkeypatch.setattr(ws_listener, "_load_inbox_sync_since", lambda *args, **kwargs: None)
    monkeypatch.setattr(ws_listener, "_save_inbox_sync_since", lambda *args, **kwargs: None)
    monkeypatch.setattr(ws_listener.local_store, "get_message_by_id", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ws_listener,
        "create_authenticator",
        lambda credential_name, config: (object(), {"did": "did:alice"}),
    )

    async def _fake_auto_process(
        messages: list[dict[str, Any]],
        *,
        local_did: str,
        auth: Any,
        credential_name: str,
    ) -> tuple[list[dict[str, Any]], list[str], object]:
        del messages, local_did, auth, credential_name
        return (
            [
                {
                    "id": "cipher-1",
                    "type": "text",
                    "sender_did": "did:bob",
                    "content": "secret hello",
                    "_e2ee": True,
                    "created_at": "2026-03-20T10:01:00+00:00",
                }
            ],
            ["cipher-1"],
            object(),
        )

    monkeypatch.setattr(ws_listener, "_auto_process_e2ee_messages", _fake_auto_process)
    monkeypatch.setattr(ws_listener, "_store_inbox_messages", lambda credential_name, my_did, inbox: stored_payloads.append(inbox))
    monkeypatch.setattr(
        ws_listener,
        "_mark_local_messages_read",
        lambda *, credential_name, owner_did, message_ids: None,
    )
    monkeypatch.setattr(ws_listener, "classify_message", lambda params, my_did, cfg: None)

    asyncio.run(
        ws_listener._catch_up_inbox(
            credential_name="default",
            my_did="did:alice",
            cfg=object(),
            config=config,
            ws=fake_ws,
            http=object(),
            local_db=object(),
            channels=[],
        )
    )

    assert stored_payloads == [[
        {
            "id": "cipher-1",
            "type": "text",
            "sender_did": "did:bob",
            "content": "secret hello",
            "_e2ee": True,
            "created_at": "2026-03-20T10:01:00+00:00",
        }
    ]]
