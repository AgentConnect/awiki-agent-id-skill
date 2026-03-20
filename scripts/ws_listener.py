#!/usr/bin/env python3
"""WebSocket listener: long-running background process that receives molt-message pushes and routes to webhooks.

[INPUT]: credential_store (DID identity), SDKConfig, WsClient, ListenerConfig,
         E2eeHandler, service_manager, local_store, logging_config
[OUTPUT]: WebSocket -> OpenClaw TUI bridge (chat.inject RPC for instant display,
          HTTP webhook fallback) + cross-platform service lifecycle
          management + local SQLite message/group persistence
[POS]: Standalone background process with cross-platform service management (launchd / systemd / Task Scheduler), reuses utils/ core tool layer

[PROTOCOL]:
1. Update this header when logic changes
2. Check the folder's CLAUDE.md after updating

Core pipeline:
  molt-message WS push -> listener receives -> E2EE intercept/decrypt -> route classification -> chat.inject to TUI

Subcommands:
  run       Run in foreground (for debugging)
  install   Install background service and start
  uninstall Uninstall background service
  start     Start an installed service
  stop      Stop a running service
  status    Show service status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure scripts/ is in sys.path (consistent with other scripts)
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import httpx

from check_inbox import (
    MESSAGE_RPC,
    _auto_process_e2ee_messages,
    _message_sort_key,
    _store_inbox_messages,
)
from credential_store import create_authenticator, load_identity, update_jwt
from credential_layout import (
    ensure_credential_directory,
    resolve_credential_paths,
    write_secure_json,
)
from e2ee_handler import E2eeHandler
from listener_config import ROUTING_MODES, ListenerConfig
from utils.config import SDKConfig
from utils.identity import DIDIdentity
from utils.client import create_molt_message_client
from utils.rpc import authenticated_rpc_call
from utils.logging_config import configure_logging
from utils.ws import WsClient

import local_store

logger = logging.getLogger("ws_listener")


# --- Utility Functions --------------------------------------------------------

def _truncate_did(did: str) -> str:
    """Abbreviate DID for display (first and last 8 characters)."""
    if len(did) <= 20:
        return did
    return f"{did[:8]}...{did[-8:]}"


def _is_reserved_e2ee_type(msg_type: str) -> bool:
    """Return whether the message type belongs to raw E2EE transport data."""
    return (
        msg_type == "e2ee"
        or msg_type.startswith("e2ee_")
        or msg_type.startswith("group_e2ee_")
        or msg_type == "group_epoch_advance"
    )


# --- Route Classification ----------------------------------------------------

def classify_message(
    params: dict[str, Any],
    my_did: str,
    cfg: ListenerConfig,
) -> str | None:
    """Classify a message for routing.

    Args:
        params: The params field from a WebSocket push notification.
        my_did: The DID of the current listener itself.
        cfg: Listener configuration.

    Returns:
        "agent" -- high priority, trigger agent turn immediately.
        "wake"  -- low priority, deferred aggregation.
        None    -- drop, do not forward.
    """
    sender_did = params.get("sender_did", "")
    content = params.get("content", "")
    msg_type = params.get("type", "text")
    group_did = params.get("group_did")
    group_id = params.get("group_id")
    is_private = group_did is None and group_id is None

    # === Drop conditions (common to all modes) ===
    if sender_did == my_did:
        return None
    if msg_type in cfg.ignore_types or _is_reserved_e2ee_type(msg_type):
        return None
    if sender_did in cfg.routing.blacklist_dids:
        return None

    # === Mode determination ===
    if cfg.mode == "agent-all":
        return "agent"
    if cfg.mode == "wake-all":
        return "wake"

    # === Smart mode: rule engine (any match -> agent) ===
    if sender_did in cfg.routing.whitelist_dids:
        return "agent"
    if is_private and cfg.routing.private_always_agent:
        return "agent"
    if isinstance(content, str) and content.startswith(cfg.routing.command_prefix):
        return "agent"
    if isinstance(content, str):
        for name in cfg.routing.bot_names:
            if name and name in content:
                return "agent"
        for kw in cfg.routing.keywords:
            if kw in content:
                return "agent"

    # === Default: Wake ===
    return "wake"


# --- Forwarding + Heartbeat --------------------------------------------------

# Path to the openclaw CLI binary
_OPENCLAW_BIN = (
    shutil.which("openclaw")
    or str(Path.home() / ".npm-global" / "bin" / "openclaw")
)


def _build_event_text(params: dict[str, Any], route: str, cfg: ListenerConfig) -> str:
    """Build the system event text from message params."""
    sender_did = params.get("sender_did", "unknown")
    sender = _truncate_did(sender_did)
    content = str(params.get("content", ""))
    content_preview = content[:50]
    group_did = params.get("group_did")
    is_private = group_did is None and params.get("group_id") is None
    msg_type = params.get("type", "text")

    if route == "agent":
        context = "Direct" if is_private else "Group"
        lines = [f"[Awiki New {context} Message{' (encrypted)' if params.get('_e2ee') else ''}]"]
        if params.get("sender_name"):
            lines.append(f"sender_name: {params['sender_name']}")
        if group_did:
            lines.append(f"group_did: {group_did}")
        if params.get("group_name"):
            lines.append(f"group_name: {params['group_name']}")
        if params.get("sent_at"):
            lines.append(f"sent_at: {params['sent_at']}")
        lines.append("")
        lines.append(content)
        return "\n".join(lines)
    else:
        if params.get("_e2ee"):
            return f"[IM] {sender}: [Encrypted] {content_preview}"
        return f"[IM] {sender}: {content_preview}"


_CHANNEL_ACTIVE_HOURS = 5  # only forward to channels active within this window
_CHANNEL_CACHE_FILE_NAME = "external-channels.json"
_INBOX_SYNC_FILE_NAME = "inbox-sync.json"


def _channel_cache_path(
    credential_name: str,
    config: SDKConfig | None = None,
) -> Path | None:
    """Return the cache path for external channels."""
    paths = resolve_credential_paths(credential_name, config)
    if paths is None:
        return None
    ensure_credential_directory(paths)
    return paths.credential_dir / _CHANNEL_CACHE_FILE_NAME


def _save_cached_channels(
    credential_name: str,
    channels: list[tuple[str, str]],
    config: SDKConfig | None = None,
) -> None:
    """Persist external channels to the credential directory."""
    path = _channel_cache_path(credential_name, config)
    if path is None:
        logger.debug("Skipping channel cache save; credential not found: %s", credential_name)
        return
    payload = {
        "cached_at": time.time(),
        "channels": [{"channel": ch, "target": tgt} for ch, tgt in channels],
    }
    try:
        write_secure_json(path, payload)
        logger.debug("Saved external channel cache: %s", path)
    except Exception:
        logger.debug("Failed to save external channel cache", exc_info=True)


def _load_cached_channels(
    credential_name: str,
    config: SDKConfig | None = None,
) -> tuple[list[tuple[str, str]], float | None]:
    """Load cached external channels from disk."""
    path = _channel_cache_path(credential_name, config)
    if path is None or not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_at = data.get("cached_at")
        raw_channels = data.get("channels", [])
        channels: list[tuple[str, str]] = []
        if isinstance(raw_channels, list):
            for item in raw_channels:
                if isinstance(item, dict):
                    ch = item.get("channel")
                    tgt = item.get("target")
                    if isinstance(ch, str) and isinstance(tgt, str):
                        channels.append((ch, tgt))
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    ch, tgt = item
                    if isinstance(ch, str) and isinstance(tgt, str):
                        channels.append((ch, tgt))
        return channels, cached_at if isinstance(cached_at, (int, float)) else None
    except Exception:
        logger.debug("Failed to load external channel cache", exc_info=True)
        return [], None


def _format_cached_at(ts: float | None) -> str:
    """Format cached timestamp for logs."""
    if not ts:
        return "unknown time"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


async def _refresh_external_channels(
    credential_name: str,
    config: SDKConfig | None = None,
) -> tuple[list[tuple[str, str]], str, float | None]:
    """Fetch external channels, falling back to cached channels on failure."""
    channels = await _fetch_external_channels()
    if channels:
        _save_cached_channels(credential_name, channels, config)
        return channels, "live", None
    cached, cached_at = _load_cached_channels(credential_name, config)
    if cached:
        return cached, "cache", cached_at
    return [], "empty", None


def _inbox_sync_path(
    credential_name: str,
    config: SDKConfig | None = None,
) -> Path | None:
    """Return the inbox sync state path."""
    paths = resolve_credential_paths(credential_name, config)
    if paths is None:
        return None
    ensure_credential_directory(paths)
    return paths.credential_dir / _INBOX_SYNC_FILE_NAME


def _load_inbox_sync_since(
    credential_name: str,
    config: SDKConfig | None = None,
) -> str | None:
    """Load last inbox sync timestamp (ISO string) from disk."""
    path = _inbox_sync_path(credential_name, config)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        since = data.get("since")
        return since if isinstance(since, str) and since else None
    except Exception:
        logger.debug("Failed to load inbox sync state", exc_info=True)
        return None


def _save_inbox_sync_since(
    credential_name: str,
    since: str,
    config: SDKConfig | None = None,
) -> None:
    """Persist last inbox sync timestamp (ISO string)."""
    path = _inbox_sync_path(credential_name, config)
    if path is None:
        logger.debug("Skipping inbox sync save; credential not found: %s", credential_name)
        return
    payload = {
        "since": since,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        write_secure_json(path, payload)
        logger.debug("Saved inbox sync state: %s", path)
    except Exception:
        logger.debug("Failed to save inbox sync state", exc_info=True)


def _parse_inbox_timestamp(value: Any) -> datetime | None:
    """Parse inbox timestamps into datetime (UTC-aware when possible)."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            return None
    return None


async def _catch_up_inbox(
    credential_name: str,
    my_did: str,
    cfg: ListenerConfig,
    config: SDKConfig,
    http: httpx.AsyncClient,
    local_db: Any,
    channels: list[tuple[str, str]] | None = None,
) -> None:
    """Fetch unread inbox messages after reconnect and forward them."""
    auth_result = create_authenticator(credential_name, config)
    if auth_result is None:
        logger.warning("Inbox catch-up skipped: credential '%s' not found", credential_name)
        return
    auth, _ = auth_result

    since = _load_inbox_sync_since(credential_name, config)
    params: dict[str, Any] = {"user_did": my_did, "limit": 50}
    if since:
        params["since"] = since

    try:
        async with create_molt_message_client(config) as client:
            inbox = await authenticated_rpc_call(
                client,
                MESSAGE_RPC,
                "get_inbox",
                params=params,
                auth=auth,
                credential_name=credential_name,
            )
    except Exception as exc:
        logger.warning("Inbox catch-up failed: %s", exc)
        return

    messages = inbox.get("messages", [])
    if not messages:
        logger.info("Inbox catch-up: no messages (since=%s)", since or "none")
        return

    messages.sort(key=_message_sort_key)
    to_process: list[dict[str, Any]] = []
    for msg in messages:
        msg_id = msg.get("id") or msg.get("msg_id")
        if not msg_id:
            continue
        try:
            existing = local_store.get_message_by_id(
                local_db,
                msg_id=str(msg_id),
                owner_did=my_did,
            )
        except Exception:
            existing = None
        if existing is None:
            to_process.append(msg)

    logger.info(
        "Inbox catch-up: fetched=%d new=%d since=%s",
        len(messages),
        len(to_process),
        since or "none",
    )

    # Persist all fetched messages locally (offline backfill)
    _store_inbox_messages(credential_name, my_did, inbox)

    # Advance sync cursor to newest created_at
    max_created_at: datetime | None = None
    for msg in messages:
        ts = _parse_inbox_timestamp(msg.get("created_at") or msg.get("sent_at"))
        if ts is None:
            continue
        if max_created_at is None or ts > max_created_at:
            max_created_at = ts
    if max_created_at is not None:
        _save_inbox_sync_since(
            credential_name,
            max_created_at.astimezone(timezone.utc).isoformat(),
            config,
        )

    if not to_process:
        return

    rendered_messages, _, _ = await _auto_process_e2ee_messages(
        to_process,
        local_did=my_did,
        auth=auth,
        credential_name=credential_name,
    )

    msg_seq = 0
    for msg in rendered_messages:
        route = classify_message(msg, my_did, cfg)
        if route is None:
            continue
        msg_seq += 1
        logger.info(
            "[catch-up #%d] Forwarding: route=%s sender=%s",
            msg_seq,
            route,
            _truncate_did(msg.get("sender_did", "")),
        )
        await _forward(http, cfg.wake_webhook_url, cfg.webhook_token, msg, route, cfg, channels, msg_seq)

    # Cursor already advanced above.


async def _fetch_external_channels() -> list[tuple[str, str]]:
    """Query OpenClaw gateway for active external channel sessions.

    Returns list of (channel, target) tuples parsed from session keys.
    Only includes channels active within the last ``_CHANNEL_ACTIVE_HOURS`` hours.
    Filters out TUI (key ends with :main) and hook sessions.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            _OPENCLAW_BIN, "gateway", "call", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            stderr_str = stderr.decode().strip() if stderr else ""
            logger.warning(
                "gateway status failed: exit=%d stderr=%s",
                proc.returncode, stderr_str[:200],
            )
            return []
        stdout_str = stdout.decode()
        # openclaw prints plugin logs to stdout before JSON; find the JSON object
        json_start = stdout_str.find("{")
        if json_start < 0:
            logger.warning("gateway status: no JSON in output")
            return []
        data = json.loads(stdout_str[json_start:])
        now_ms = time.time() * 1000
        max_age_ms = _CHANNEL_ACTIVE_HOURS * 3600 * 1000
        best_channel: tuple[str, str] | None = None
        best_updated_at = -1.0
        for session in data.get("sessions", {}).get("recent", []):
            key = session.get("key", "")
            # Skip TUI and hook sessions
            if key.endswith(":main") or "hook:" in key:
                continue
            # Check activity window
            updated_at = session.get("updatedAt", 0)
            age_ms = now_ms - updated_at
            if age_ms > max_age_ms:
                logger.debug("Skipping stale channel: %s (age=%.1fh)", key, age_ms / 3600000)
                continue
            # Parse: agent:<agentId>:<channel>:<type>:<target...>
            parts = key.split(":")
            if len(parts) >= 5:
                channel = parts[2]
                target = ":".join(parts[4:])
                if updated_at >= best_updated_at:
                    best_updated_at = updated_at
                    best_channel = (channel, target)
        return [best_channel] if best_channel else []
    except FileNotFoundError:
        logger.warning("openclaw CLI not found at %s", _OPENCLAW_BIN)
        return []
    except Exception as exc:
        logger.warning("Failed to fetch external channels: %s", exc)
        return []


async def _send_to_channels(text: str, channels: list[tuple[str, str]], msg_seq: int = 0) -> None:
    """Forward message text to external channels via openclaw message send."""
    tag = f"[#{msg_seq}] " if msg_seq else ""
    for channel, target in channels:
        try:
            proc = await asyncio.create_subprocess_exec(
                _OPENCLAW_BIN, "message", "send",
                "--channel", channel,
                "--target", target,
                "--message", text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                logger.info("%s%s OK -> %s", tag, channel, target)
            else:
                stderr_str = stderr.decode().strip() if stderr else ""
                logger.warning(
                    "%s%s FAIL -> %s exit=%d stderr=%s",
                    tag, channel, target, proc.returncode, stderr_str[:200],
                )
        except asyncio.TimeoutError:
            logger.warning("%s%s FAIL -> %s timeout", tag, channel, target)
        except FileNotFoundError:
            logger.warning("openclaw CLI not found at %s", _OPENCLAW_BIN)
            break
        except Exception as exc:
            logger.error("%s%s FAIL -> %s: %s", tag, channel, target, exc)


async def _forward(
    http: httpx.AsyncClient,
    url: str,
    token: str,
    params: dict[str, Any],
    route: str,
    cfg: ListenerConfig,
    channels: list[tuple[str, str]] | None = None,
    msg_seq: int = 0,
) -> bool:
    """Forward a message to OpenClaw via ``chat.inject`` + HTTP ``/hooks/wake`` + external channels."""
    e2ee_tag = "[E2EE] " if params.get("_e2ee") else ""
    sender = _truncate_did(params.get("sender_did", "unknown"))
    text = _build_event_text(params, route, cfg)
    tag = f"[#{msg_seq}] " if msg_seq else ""

    # Primary: chat.inject (direct TUI injection, no model call)
    inject_ok = False
    inject_params = json.dumps(
        {"sessionKey": "agent:main:main", "message": text},
        ensure_ascii=False,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            _OPENCLAW_BIN, "gateway", "call", "chat.inject",
            "--params", inject_params, "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        stdout_str = stdout.decode().strip() if stdout else ""

        if proc.returncode == 0 and "ok" in stdout_str.lower():
            logger.info(
                "%s%sTUI OK (chat.inject) sender=%s",
                tag, e2ee_tag, sender,
            )
            inject_ok = True
        else:
            stderr_str = stderr.decode().strip() if stderr else ""
            logger.warning(
                "%s%sTUI FAIL (chat.inject) exit=%d stderr=%s",
                tag, e2ee_tag, proc.returncode, stderr_str[:200],
            )
    except asyncio.TimeoutError:
        logger.warning("%sTUI FAIL (chat.inject) timeout", tag)
    except FileNotFoundError:
        logger.warning("openclaw CLI not found at %s", _OPENCLAW_BIN)
    except Exception as exc:
        logger.error("%sTUI FAIL (chat.inject) %s", tag, exc)

    # # HTTP /hooks/wake — triggers heartbeat (disabled: TUI + channel send cover delivery)
    # headers: dict[str, str] = {"Content-Type": "application/json"}
    # if token:
    #     headers["Authorization"] = f"Bearer {token}"
    # body = {"text": text, "mode": "now"}
    # try:
    #     resp = await http.post(url, json=body, headers=headers)
    #     if resp.is_success:
    #         logger.info(
    #             "%s%sWake OK [%d] sender=%s",
    #             tag, e2ee_tag, resp.status_code, sender,
    #         )
    #     else:
    #         logger.warning(
    #             "%s%sWake FAIL [%d] %s",
    #             tag, e2ee_tag, resp.status_code, resp.text[:200],
    #         )
    # except httpx.HTTPError as exc:
    #     logger.warning("%sWake FAIL %s", tag, exc)

    # External channels: forward to Feishu, Telegram, etc.
    if channels:
        await _send_to_channels(text, channels, msg_seq)
    elif channels is not None:
        logger.debug("%sNo external channels; skip send", tag)

    return inject_ok


async def _heartbeat_task(ws: WsClient, interval: float, ping_event: asyncio.Event) -> None:
    """Periodically signal the main loop to send a heartbeat ping.

    Instead of calling ws.ping() directly (which races with the main loop's
    recv), this task simply sets an event flag.  The main loop checks the flag
    during its idle timeout and performs the actual ping/pong in-band.
    """
    while True:
        await asyncio.sleep(interval)
        ping_event.set()


# --- Identity + JWT -----------------------------------------------------------

def _build_identity(cred_data: dict[str, Any]) -> DIDIdentity:
    """Build a DIDIdentity from credential data."""
    private_key_pem = cred_data["private_key_pem"]
    if isinstance(private_key_pem, str):
        private_key_pem = private_key_pem.encode("utf-8")
    public_key_pem = cred_data.get("public_key_pem", b"")
    if isinstance(public_key_pem, str):
        public_key_pem = public_key_pem.encode("utf-8")

    return DIDIdentity(
        did=cred_data["did"],
        did_document=cred_data.get("did_document", {}),
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
        user_id=cred_data.get("user_id"),
        jwt_token=cred_data.get("jwt_token"),
    )


async def _refresh_jwt(
    credential_name: str,
    config: SDKConfig,
) -> str | None:
    """Attempt to refresh JWT via WBA authentication."""
    result = create_authenticator(credential_name, config)
    if result is None:
        return None
    auth, cred_data = result

    try:
        from utils.auth import get_jwt_via_wba
        from utils.client import create_user_service_client

        identity = _build_identity(cred_data)
        async with create_user_service_client(config) as client:
            token = await get_jwt_via_wba(client, identity, config.did_domain)
            update_jwt(credential_name, token)
            return token
    except Exception as exc:
        logger.error("JWT refresh failed: %s", exc)
        return None


# --- Main Listen Loop ---------------------------------------------------------

async def listen_loop(
    credential_name: str,
    cfg: ListenerConfig,
    config: SDKConfig | None = None,
) -> None:
    """Main listen loop. Infinite loop: connect -> receive -> classify -> forward, with automatic reconnection."""
    if config is None:
        config = SDKConfig()

    delay = cfg.reconnect_base_delay

    # E2EE handler initialization (always enabled)
    e2ee_handler: E2eeHandler | None = E2eeHandler(
        credential_name,
        save_interval=cfg.e2ee_save_interval,
        decrypt_fail_action=cfg.e2ee_decrypt_fail_action,
    )

    # Local SQLite storage initialization
    local_db = local_store.get_connection()
    local_store.ensure_schema(local_db)

    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as http:
        while True:
            cred_data = load_identity(credential_name)
            if cred_data is None:
                logger.error("Credential '%s' not found, retrying in %.0fs", credential_name, delay)
                await asyncio.sleep(delay)
                continue

            identity = _build_identity(cred_data)
            my_did = identity.did

            if not identity.jwt_token:
                logger.warning("Credential missing JWT, attempting refresh...")
                token = await _refresh_jwt(credential_name, config)
                if token:
                    identity.jwt_token = token
                else:
                    logger.error("JWT acquisition failed, retrying in %.0fs", delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, cfg.reconnect_max_delay)
                    continue

            # E2EE handler lazy initialization (requires my_did)
            if e2ee_handler is not None and not e2ee_handler.is_ready:
                if not await e2ee_handler.initialize(my_did):
                    logger.warning("E2EE initialization failed, running in non-E2EE mode")
                    e2ee_handler = None

            logger.info("Connecting to WebSocket... DID=%s mode=%s e2ee=True",
                        _truncate_did(my_did), cfg.mode)

            heartbeat: asyncio.Task | None = None
            try:
                async with WsClient(config, identity) as ws:
                    delay = cfg.reconnect_base_delay
                    logger.info("WebSocket connected successfully")

                    # Discover external channels (Feishu, Telegram, etc.)
                    ext_channels, ext_source, ext_cached_at = await _refresh_external_channels(
                        credential_name,
                        config,
                    )
                    if ext_channels:
                        suffix = (
                            ""
                            if ext_source == "live"
                            else f" (cache: {_format_cached_at(ext_cached_at)})"
                        )
                        logger.info(
                            "External channels ready%s: %s",
                            suffix,
                            ", ".join(f"{ch}:{tgt}" for ch, tgt in ext_channels),
                        )
                    else:
                        logger.info("No external channels available at connect")
                    last_channel_refresh = time.monotonic()
                    channel_refresh_interval = 300.0  # seconds

                    # Catch up on inbox messages missed during disconnects.
                    await _catch_up_inbox(
                        credential_name,
                        my_did,
                        cfg,
                        config,
                        http,
                        local_db,
                        ext_channels,
                    )

                    ping_event = asyncio.Event()
                    heartbeat = asyncio.create_task(
                        _heartbeat_task(ws, cfg.heartbeat_interval, ping_event),
                    )

                    msg_seq = 0  # per-connection message sequence number

                    while True:
                        notification = await ws.receive_notification(timeout=5.0)
                        if notification is None:
                            # Idle timeout — check if heartbeat ping is due
                            if ping_event.is_set():
                                ping_event.clear()
                                try:
                                    ok = await ws.ping()
                                    if ok:
                                        logger.debug("Heartbeat pong OK")
                                    else:
                                        logger.warning("Heartbeat pong abnormal")
                                except Exception as exc:
                                    logger.warning("Heartbeat failed: %s", exc)
                                    raise
                            # Periodically refresh external channels
                            now = time.monotonic()
                            if now - last_channel_refresh >= channel_refresh_interval:
                                ext_channels, ext_source, ext_cached_at = await _refresh_external_channels(
                                    credential_name,
                                    config,
                                )
                                if ext_channels:
                                    suffix = (
                                        ""
                                        if ext_source == "live"
                                        else f" (cache: {_format_cached_at(ext_cached_at)})"
                                    )
                                    logger.info(
                                        "External channels refreshed%s: %s",
                                        suffix,
                                        ", ".join(f"{ch}:{tgt}" for ch, tgt in ext_channels),
                                    )
                                else:
                                    logger.info("External channels refresh returned empty")
                                last_channel_refresh = now
                            if e2ee_handler is not None:
                                await e2ee_handler.maybe_save_state()
                            continue

                        method = notification.get("method", "")
                        if method in ("ping", "pong"):
                            if method == "ping":
                                try:
                                    await ws.send_pong()
                                    logger.debug("Replied pong to server ping")
                                except Exception as exc:
                                    logger.warning("Failed to send pong: %s", exc)
                                    raise
                            continue
                        if method != "new_message":
                            logger.debug("Ignoring non-message notification: method=%s", method)
                            continue

                        params = notification.get("params", {})
                        msg_type = params.get("type", "text")
                        sender_did = params.get("sender_did", "")
                        msg_seq += 1
                        content_preview = str(params.get("content", ""))[:80]
                        logger.info(
                            "[#%d] Received: type=%s sender=%s content=%s",
                            msg_seq, msg_type, _truncate_did(sender_did), content_preview,
                        )

                        # E2EE message interception (before classify_message)
                        if (e2ee_handler is not None
                                and e2ee_handler.is_ready
                                and e2ee_handler.is_e2ee_type(msg_type)):
                            if sender_did == my_did:
                                logger.debug("Skipping self-sent E2EE message")
                                continue

                            if e2ee_handler.is_protocol_type(msg_type):
                                responses = await e2ee_handler.handle_protocol_message(params)
                                if responses:
                                    logger.info(
                                        "E2EE protocol handled: type=%s sender=%s responses=%d",
                                        msg_type, _truncate_did(sender_did), len(responses),
                                    )
                                    for resp_type, resp_content in responses:
                                        await ws.send_message(
                                            receiver_did=sender_did,
                                            content=json.dumps(resp_content),
                                            msg_type=resp_type,
                                        )
                                await e2ee_handler.force_save_state()
                                continue

                            if msg_type == "e2ee_msg":
                                result = await e2ee_handler.decrypt_message(params)
                                if result.error_responses:
                                    logger.warning(
                                        "E2EE decrypt failed, sending error: sender=%s errors=%d",
                                        _truncate_did(sender_did), len(result.error_responses),
                                    )
                                    for resp_type, resp_content in result.error_responses:
                                        await ws.send_message(
                                            receiver_did=sender_did,
                                            content=json.dumps(resp_content),
                                            msg_type=resp_type,
                                        )
                                if result.params is None:
                                    logger.warning(
                                        "E2EE decrypt dropped: sender=%s",
                                        _truncate_did(sender_did),
                                    )
                                    await e2ee_handler.maybe_save_state()
                                    continue
                                params = result.params
                                logger.info(
                                    "E2EE decrypt OK: sender=%s type=%s content_len=%d",
                                    _truncate_did(sender_did), params.get("type", ""),
                                    len(str(params.get("content", ""))),
                                )
                                await e2ee_handler.maybe_save_state()

                        # Original routing logic
                        route = classify_message(params, my_did, cfg)
                        logger.info(
                            "[#%d] Route: %s sender=%s type=%s e2ee=%s",
                            msg_seq, route or "DROP", _truncate_did(params.get("sender_did", "")),
                            params.get("type", ""), bool(params.get("_e2ee")),
                        )

                        # Store message locally before routing
                        try:
                            sender_did = params.get("sender_did", "")
                            await asyncio.to_thread(
                                local_store.store_message,
                                local_db,
                                msg_id=params.get("id", ""),
                                owner_did=my_did,
                                thread_id=local_store.make_thread_id(
                                    my_did,
                                    peer_did=sender_did,
                                    group_id=params.get("group_id"),
                                ),
                                direction=0,
                                sender_did=sender_did,
                                receiver_did=params.get("receiver_did"),
                                group_id=params.get("group_id"),
                                group_did=params.get("group_did"),
                                content_type=params.get("type", "text"),
                                content=str(params.get("content", "")),
                                title=params.get("title"),
                                server_seq=params.get("server_seq"),
                                sent_at=params.get("sent_at"),
                                is_e2ee=bool(params.get("_e2ee")),
                                sender_name=params.get("sender_name"),
                                metadata=(
                                    json.dumps(
                                        {"system_event": params.get("system_event")},
                                        ensure_ascii=False,
                                    )
                                    if params.get("system_event") is not None
                                    else None
                                ),
                                credential_name=credential_name,
                            )
                            if params.get("group_id"):
                                await asyncio.to_thread(
                                    local_store.upsert_group,
                                    local_db,
                                    owner_did=my_did,
                                    group_id=str(params.get("group_id", "")),
                                    group_did=params.get("group_did"),
                                    name=params.get("group_name"),
                                    membership_status="active",
                                    last_synced_seq=params.get("server_seq"),
                                    last_message_at=params.get("sent_at"),
                                    credential_name=credential_name,
                                )
                            if params.get("group_id") and isinstance(params.get("system_event"), dict):
                                await asyncio.to_thread(
                                    local_store.sync_group_member_from_system_event,
                                    local_db,
                                    owner_did=my_did,
                                    group_id=str(params.get("group_id", "")),
                                    system_event=params.get("system_event"),
                                    credential_name=credential_name,
                                )
                            # Record sender in contacts
                            if sender_did:
                                await asyncio.to_thread(
                                    local_store.upsert_contact,
                                    local_db,
                                    owner_did=my_did,
                                    did=sender_did,
                                    name=params.get("sender_name"),
                                )
                        except Exception:
                            logger.debug("Failed to store message locally", exc_info=True)

                        if route is None:
                            logger.info(
                                "[#%d] Dropped: sender=%s type=%s",
                                msg_seq, _truncate_did(params.get("sender_did", "")),
                                params.get("type", ""),
                            )
                            continue

                        # All routes now use /hooks/wake to inject into main session
                        url = cfg.wake_webhook_url
                        logger.info(
                            "[#%d] Forwarding: route=%s sender=%s",
                            msg_seq, route, _truncate_did(params.get("sender_did", "")),
                        )
                        # If channels are empty, attempt a quick refresh before send
                        if not ext_channels:
                            ext_channels, ext_source, ext_cached_at = await _refresh_external_channels(
                                credential_name,
                                config,
                            )
                            if ext_channels:
                                suffix = (
                                    ""
                                    if ext_source == "live"
                                    else f" (cache: {_format_cached_at(ext_cached_at)})"
                                )
                                logger.info(
                                    "External channels ready (on-demand)%s: %s",
                                    suffix,
                                    ", ".join(f"{ch}:{tgt}" for ch, tgt in ext_channels),
                                )
                            else:
                                logger.info("External channels empty (on-demand refresh)")
                            last_channel_refresh = time.monotonic()
                        await _forward(http, url, cfg.webhook_token, params, route, cfg, ext_channels, msg_seq)

            except asyncio.CancelledError:
                if e2ee_handler is not None:
                    await e2ee_handler.force_save_state()
                local_db.close()
                logger.info("Listen loop cancelled")
                raise
            except Exception as exc:
                logger.warning("Connection lost: %s, reconnecting in %.0fs", exc, delay)
            finally:
                if heartbeat and not heartbeat.done():
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except (asyncio.CancelledError, Exception):
                        pass
                if e2ee_handler is not None:
                    await e2ee_handler.force_save_state()

            new_token = await _refresh_jwt(credential_name, config)
            if new_token:
                logger.info("JWT refreshed")

            await asyncio.sleep(delay)
            delay = min(delay * 2, cfg.reconnect_max_delay)


# --- Service Lifecycle (delegates to service_manager) -------------------------

def cmd_install(args: argparse.Namespace) -> None:
    """Install and start the background service."""
    from service_manager import get_service_manager
    get_service_manager().install(args.credential, args.config, args.mode)


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Uninstall the background service."""
    from service_manager import get_service_manager
    get_service_manager().uninstall()


def cmd_start(args: argparse.Namespace) -> None:
    """Start an installed service."""
    from service_manager import get_service_manager
    get_service_manager().start()


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop a running service."""
    from service_manager import get_service_manager
    get_service_manager().stop()


def cmd_status(args: argparse.Namespace) -> None:
    """Show service status."""
    from service_manager import get_service_manager
    print(json.dumps(get_service_manager().status(), indent=2, ensure_ascii=False))


def cmd_run(args: argparse.Namespace) -> None:
    """Run the listener in foreground."""
    level = logging.DEBUG if args.verbose else logging.INFO
    log_path = configure_logging(
        level=level,
        console_level=level,
        force=True,
        mirror_stdio=True,
    )
    logger.info("Application logging enabled: %s", log_path)

    cfg = ListenerConfig.load(args.config, mode_override=args.mode)
    logger.info(
        "Config loaded: mode=%s agent=%s wake=%s",
        cfg.mode, cfg.agent_webhook_url, cfg.wake_webhook_url,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    task: asyncio.Task | None = None

    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        if task and not task.done():
            task.cancel()

    signal.signal(signal.SIGINT, _shutdown)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        task = loop.create_task(listen_loop(args.credential, cfg))
        loop.run_until_complete(task)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Listener stopped")
    finally:
        loop.close()


# --- CLI ----------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    configure_logging(console_level=None, mirror_stdio=True)

    parser = argparse.ArgumentParser(
        description="WebSocket listener: receive molt-message pushes and route to webhooks",
    )
    subparsers = parser.add_subparsers(dest="command", help="subcommands")

    # --- run ---
    p_run = subparsers.add_parser("run", help="Run in foreground (for debugging)")
    p_run.add_argument("--credential", default="default", help="Credential name")
    p_run.add_argument("--config", default=None, help="JSON config file path")
    p_run.add_argument("--mode", choices=ROUTING_MODES, default=None,
                       help="Routing mode (overrides config file)")
    p_run.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    p_run.set_defaults(func=cmd_run)

    # --- install ---
    p_install = subparsers.add_parser("install", help="Install background service and start")
    p_install.add_argument("--credential", default="default", help="Credential name")
    p_install.add_argument("--config", default=None, help="JSON config file path")
    p_install.add_argument("--mode", choices=ROUTING_MODES, default=None,
                           help="Routing mode")
    p_install.set_defaults(func=cmd_install)

    # --- uninstall ---
    p_uninstall = subparsers.add_parser("uninstall", help="Uninstall background service")
    p_uninstall.set_defaults(func=cmd_uninstall)

    # --- start ---
    p_start = subparsers.add_parser("start", help="Start an installed service")
    p_start.set_defaults(func=cmd_start)

    # --- stop ---
    p_stop = subparsers.add_parser("stop", help="Stop a running service")
    p_stop.set_defaults(func=cmd_stop)

    # --- status ---
    p_status = subparsers.add_parser("status", help="Show service status")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    logger.info("ws_listener CLI started command=%s", args.command)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
