"""Unified status check: local upgrade + identity verification + inbox/group summary.

Usage:
    python scripts/check_status.py                     # Status check with E2EE auto-processing
    python scripts/check_status.py --no-auto-e2ee      # Disable E2EE auto-processing
    python scripts/check_status.py --credential alice   # Specify credential
    python scripts/check_status.py --upgrade-only       # Run local upgrade and exit

[INPUT]: SDK (RPC calls, E2eeClient), credential_store (authenticator factory),
         e2ee_store, credential_migration, database_migration, local_store,
         logging_config
[OUTPUT]: Structured JSON status report (local upgrade + identity + inbox +
          group_watch + e2ee_auto + e2ee_sessions), with inbox refreshed
          after optional auto-processing
[POS]: Unified status check entry point for Agent session startup and heartbeat calls
       with default-on, server_seq-aware E2EE auto-processing and local
       discovery-group watch summaries

[PROTOCOL]:
1. Update this header when logic changes
2. Check the folder's CLAUDE.md after updating
"""

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from utils import (
    SDKConfig,
    E2eeClient,
    create_user_service_client,
    create_molt_message_client,
    authenticated_rpc_call,
)
from utils.logging_config import configure_logging
import local_store
from credential_migration import ensure_credential_storage_ready
from database_migration import ensure_local_database_ready
from credential_store import load_identity, create_authenticator
from e2ee_store import load_e2ee_state, save_e2ee_state
from e2ee_outbox import record_remote_failure


MESSAGE_RPC = "/message/rpc"
AUTH_RPC = "/user-service/did-auth/rpc"
logger = logging.getLogger(__name__)

# E2EE protocol message types
_E2EE_HANDSHAKE_TYPES = {"e2ee_init", "e2ee_ack", "e2ee_rekey", "e2ee_error"}
_E2EE_SESSION_SETUP_TYPES = {"e2ee_init", "e2ee_rekey"}
_E2EE_MSG_TYPES = {"e2ee_init", "e2ee_ack", "e2ee_msg", "e2ee_rekey", "e2ee_error"}
_E2EE_TYPE_ORDER = {
    "e2ee_init": 0,
    "e2ee_ack": 1,
    "e2ee_rekey": 2,
    "e2ee_msg": 3,
    "e2ee_error": 4,
}


def _message_time_value(message: dict[str, Any]) -> str:
    """Return a sortable timestamp string for one message."""
    timestamp = message.get("sent_at") or message.get("created_at")
    return timestamp if isinstance(timestamp, str) else ""


def ensure_local_upgrade_ready(credential_name: str = "default") -> dict[str, Any]:
    """Run local credential/database upgrades needed by the current skill version."""
    credential_layout = ensure_credential_storage_ready(credential_name)
    local_database = ensure_local_database_ready()
    ready = (
        credential_layout.get("credential_ready", False)
        and local_database.get("status") != "error"
    )

    performed: list[str] = []
    migration = credential_layout.get("migration")
    if isinstance(migration, dict) and migration.get("status") not in {
        None,
        "not_needed",
    }:
        performed.append("credential_layout")
    if local_database.get("status") == "migrated":
        performed.append("local_database")

    return {
        "status": "ready" if ready else "error",
        "credential_ready": credential_layout.get("credential_ready", False),
        "database_ready": local_database.get("status") != "error",
        "performed": performed,
        "credential_layout": credential_layout,
        "local_database": local_database,
    }


def _message_sort_key(message: dict[str, Any]) -> tuple[Any, ...]:
    """Build a stable E2EE inbox ordering key with server_seq priority inside a sender stream."""
    sender_did = message.get("sender_did", "")
    server_seq = message.get("server_seq")
    has_server_seq = 0 if isinstance(server_seq, int) else 1
    server_seq_value = server_seq if isinstance(server_seq, int) else 0
    return (
        sender_did,
        has_server_seq,
        server_seq_value,
        _message_time_value(message),
        _E2EE_TYPE_ORDER.get(message.get("type"), 99),
    )


def _is_user_visible_message_type(msg_type: str) -> bool:
    """Return whether a message type should be exposed to end users."""
    return msg_type not in _E2EE_MSG_TYPES


def summarize_group_watch(owner_did: str | None) -> dict[str, Any]:
    """Summarize locally tracked discovery groups for heartbeat decisions."""
    if not owner_did:
        return {"status": "no_identity", "active_groups": 0, "groups": []}

    try:
        conn = local_store.get_connection()
        try:
            local_store.ensure_schema(conn)
            group_rows = conn.execute(
                """
                SELECT
                    group_id,
                    name,
                    slug,
                    my_role,
                    member_count,
                    group_owner_did,
                    group_owner_handle,
                    last_synced_seq,
                    last_read_seq,
                    last_message_at,
                    stored_at
                FROM groups
                WHERE owner_did = ? AND membership_status = 'active'
                ORDER BY COALESCE(last_message_at, stored_at) DESC, stored_at DESC
                LIMIT 20
                """,
                (owner_did,),
            ).fetchall()

            groups: list[dict[str, Any]] = []
            groups_with_pending_recommendations = 0
            for row in group_rows:
                group_id = row["group_id"]
                tracked_members_row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS cnt,
                        MAX(joined_at) AS latest_joined_at
                    FROM group_members
                    WHERE owner_did = ? AND group_id = ? AND status = 'active'
                    """,
                    (owner_did, group_id),
                ).fetchone()
                owner_message_row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS cnt,
                        MAX(sent_at) AS latest_sent_at
                    FROM messages
                    WHERE owner_did = ?
                      AND group_id = ?
                      AND content_type = 'group_user'
                      AND sender_did = COALESCE(?, '')
                    """,
                    (owner_did, group_id, row["group_owner_did"]),
                ).fetchone()
                local_user_message_row = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM messages
                    WHERE owner_did = ? AND group_id = ? AND content_type = 'group_user'
                    """,
                    (owner_did, group_id),
                ).fetchone()
                recommendation_row = conn.execute(
                    """
                    SELECT
                        SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                        MAX(created_at) AS last_recommended_at
                    FROM relationship_events
                    WHERE owner_did = ?
                      AND source_group_id = ?
                      AND event_type = 'ai_recommended'
                    """,
                    (owner_did, group_id),
                ).fetchone()
                saved_contact_row = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM contacts
                    WHERE owner_did = ? AND source_group_id = ?
                    """,
                    (owner_did, group_id),
                ).fetchone()

                pending_recommendations = int(
                    recommendation_row["pending_count"] or 0
                )
                if pending_recommendations > 0:
                    groups_with_pending_recommendations += 1

                local_group_user_messages = int(local_user_message_row["cnt"] or 0)
                tracked_active_members = int(tracked_members_row["cnt"] or 0)
                groups.append(
                    {
                        "group_id": group_id,
                        "name": row["name"],
                        "slug": row["slug"],
                        "my_role": row["my_role"],
                        "member_count": row["member_count"],
                        "tracked_active_members": tracked_active_members,
                        "group_owner_did": row["group_owner_did"],
                        "group_owner_handle": row["group_owner_handle"],
                        "local_group_user_messages": local_group_user_messages,
                        "local_owner_messages": int(owner_message_row["cnt"] or 0),
                        "latest_owner_message_at": owner_message_row["latest_sent_at"],
                        "latest_member_joined_at": tracked_members_row["latest_joined_at"],
                        "pending_recommendations": pending_recommendations,
                        "last_recommended_at": recommendation_row["last_recommended_at"],
                        "saved_contacts": int(saved_contact_row["cnt"] or 0),
                        "recommendation_signal_ready": (
                            tracked_active_members >= 5
                            or local_group_user_messages >= 5
                        ),
                        "last_synced_seq": row["last_synced_seq"],
                        "last_read_seq": row["last_read_seq"],
                        "last_message_at": row["last_message_at"],
                        "stored_at": row["stored_at"],
                    }
                )

            return {
                "status": "ok",
                "active_groups": len(groups),
                "groups_with_pending_recommendations": (
                    groups_with_pending_recommendations
                ),
                "groups": groups,
            }
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "active_groups": 0,
            "groups": [],
            "error": str(exc),
        }


# ---------- E2EE helpers ----------


def _load_or_create_e2ee_client(local_did: str, credential_name: str) -> E2eeClient:
    """Load existing E2EE client state from disk, or create a new client if absent."""
    # Load E2EE keys from credential
    cred = load_identity(credential_name)
    signing_pem: str | None = None
    x25519_pem: str | None = None
    if cred is not None:
        signing_pem = cred.get("e2ee_signing_private_pem")
        x25519_pem = cred.get("e2ee_agreement_private_pem")

    state = load_e2ee_state(credential_name)
    if state is not None and state.get("local_did") == local_did:
        if signing_pem is not None:
            state["signing_pem"] = signing_pem
        if x25519_pem is not None:
            state["x25519_pem"] = x25519_pem
        return E2eeClient.from_state(state)

    return E2eeClient(local_did, signing_pem=signing_pem, x25519_pem=x25519_pem)


def _save_e2ee_client(client: E2eeClient, credential_name: str) -> None:
    """Save E2EE client state to disk."""
    save_e2ee_state(client.export_state(), credential_name)


async def _send_msg(
    http_client,
    sender_did,
    receiver_did,
    msg_type,
    content,
    *,
    auth,
    credential_name="default",
):
    """Send a message (E2EE or plain)."""
    if isinstance(content, dict):
        content = json.dumps(content)
    return await authenticated_rpc_call(
        http_client,
        MESSAGE_RPC,
        "send",
        params={
            "sender_did": sender_did,
            "receiver_did": receiver_did,
            "content": content,
            "type": msg_type,
        },
        auth=auth,
        credential_name=credential_name,
    )


# ---------- Core functions ----------


async def check_identity(credential_name: str = "default") -> dict[str, Any]:
    """Check identity status; automatically refresh expired JWT."""
    data = load_identity(credential_name)
    if data is None:
        return {"status": "no_identity", "did": None, "name": None, "jwt_valid": False}

    result: dict[str, Any] = {
        "status": "ok",
        "did": data["did"],
        "name": data.get("name"),
        "jwt_valid": False,
    }

    if not data.get("jwt_token"):
        result["status"] = "no_jwt"
        return result

    config = SDKConfig()
    auth_result = create_authenticator(credential_name, config)
    if auth_result is None:
        result["status"] = "no_did_document"
        result["error"] = "Credential missing DID document; please recreate identity"
        return result

    auth, _ = auth_result
    old_token = data["jwt_token"]

    try:
        async with create_user_service_client(config) as client:
            await authenticated_rpc_call(
                client,
                AUTH_RPC,
                "get_me",
                auth=auth,
                credential_name=credential_name,
            )
            result["jwt_valid"] = True
            # Check if token was refreshed (authenticated_rpc_call auto-persists new JWT)
            refreshed_data = load_identity(credential_name)
            if refreshed_data and refreshed_data.get("jwt_token") != old_token:
                result["jwt_refreshed"] = True
    except Exception as e:
        result["status"] = "jwt_refresh_failed"
        result["error"] = str(e)

    return result


async def summarize_inbox(
    credential_name: str = "default",
) -> dict[str, Any]:
    """Fetch inbox and compute categorized statistics."""
    config = SDKConfig()
    auth_result = create_authenticator(credential_name, config)
    if auth_result is None:
        return {"status": "no_identity", "total": 0}

    auth, data = auth_result
    try:
        async with create_molt_message_client(config) as client:
            inbox = await authenticated_rpc_call(
                client,
                MESSAGE_RPC,
                "get_inbox",
                params={"user_did": data["did"], "limit": 50},
                auth=auth,
                credential_name=credential_name,
            )
    except Exception as e:
        return {"status": "error", "error": str(e), "total": 0}

    messages = inbox.get("messages", [])

    # Count only user-visible messages. Protocol and encrypted transport
    # messages are internal and should not be surfaced directly to users.
    by_type: dict[str, int] = {}
    text_by_sender: dict[str, dict[str, Any]] = {}
    text_count = 0
    visible_total = 0

    for msg in messages:
        msg_type = msg.get("type", "unknown")
        if not _is_user_visible_message_type(msg_type):
            continue

        visible_total += 1
        sender_did = msg.get("sender_did", "unknown")
        created_at = msg.get("created_at", "")

        by_type[msg_type] = by_type.get(msg_type, 0) + 1

        if msg_type == "text":
            text_count += 1
            if sender_did not in text_by_sender:
                text_by_sender[sender_did] = {"count": 0, "latest": ""}
            text_by_sender[sender_did]["count"] += 1
            if created_at > text_by_sender[sender_did]["latest"]:
                text_by_sender[sender_did]["latest"] = created_at

    return {
        "status": "ok",
        "total": visible_total,
        "by_type": by_type,
        "text_messages": text_count,
        "text_by_sender": text_by_sender,
    }


async def auto_process_e2ee(
    credential_name: str = "default",
) -> dict[str, Any]:
    """Automatically process E2EE protocol messages (init/rekey/error) in inbox."""
    config = SDKConfig()
    auth_result = create_authenticator(credential_name, config)
    if auth_result is None:
        return {"status": "no_identity", "processed": 0, "details": []}

    auth, data = auth_result
    try:
        async with create_molt_message_client(config) as client:
            # Get inbox
            inbox = await authenticated_rpc_call(
                client,
                MESSAGE_RPC,
                "get_inbox",
                params={"user_did": data["did"], "limit": 50},
                auth=auth,
                credential_name=credential_name,
            )
            messages = inbox.get("messages", [])

            # Filter E2EE protocol messages (excluding encrypted messages themselves)
            e2ee_msgs = [m for m in messages if m.get("type") in _E2EE_HANDSHAKE_TYPES]

            if not e2ee_msgs:
                return {"status": "ok"}

            # Sort by sender stream + server_seq, fallback to created_at.
            e2ee_msgs.sort(key=_message_sort_key)

            e2ee_client = _load_or_create_e2ee_client(data["did"], credential_name)
            processed_ids: list[str] = []

            for msg in e2ee_msgs:
                msg_type = msg["type"]
                sender_did = msg.get("sender_did", "")
                content = (
                    json.loads(msg["content"])
                    if isinstance(msg.get("content"), str)
                    else msg.get("content", {})
                )

                try:
                    if msg_type == "e2ee_error":
                        record_remote_failure(
                            credential_name=credential_name,
                            peer_did=sender_did,
                            content=content,
                        )
                    responses = await e2ee_client.process_e2ee_message(
                        msg_type, content
                    )
                    session_ready = True
                    terminal_error_notified = any(
                        resp_type == "e2ee_error" for resp_type, _ in responses
                    )
                    if msg_type in _E2EE_SESSION_SETUP_TYPES:
                        session_ready = e2ee_client.has_session_id(
                            content.get("session_id")
                        )
                    # Route responses to sender_did
                    for resp_type, resp_content in responses:
                        await _send_msg(
                            client,
                            data["did"],
                            sender_did,
                            resp_type,
                            resp_content,
                            auth=auth,
                            credential_name=credential_name,
                        )

                    if session_ready:
                        processed_ids.append(msg["id"])
                    elif terminal_error_notified:
                        processed_ids.append(msg["id"])
                except Exception as e:
                    logger.warning(
                        "E2EE auto-processing failed type=%s sender=%s error=%s",
                        msg_type,
                        sender_did,
                        e,
                    )

            # Mark processed messages as read
            if processed_ids:
                await authenticated_rpc_call(
                    client,
                    MESSAGE_RPC,
                    "mark_read",
                    params={"user_did": data["did"], "message_ids": processed_ids},
                    auth=auth,
                    credential_name=credential_name,
                )

            # Save E2EE state
            _save_e2ee_client(e2ee_client, credential_name)

            return {
                "status": "ok",
            }

    except Exception as e:
        return {"status": "error", "error": str(e)}


async def check_status(
    credential_name: str = "default",
    auto_e2ee: bool = True,
) -> dict[str, Any]:
    """Unified status check orchestrator."""
    report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    report["local_upgrade"] = ensure_local_upgrade_ready(credential_name)
    report["credential_layout"] = report["local_upgrade"]["credential_layout"]
    report["local_database"] = report["local_upgrade"]["local_database"]
    if not report["credential_layout"]["credential_ready"]:
        report["identity"] = {
            "status": "storage_migration_required",
            "did": None,
            "name": None,
            "jwt_valid": False,
            "error": "Credential storage migration failed or is incomplete",
        }
        report["inbox"] = {"status": "skipped", "total": 0}
        report["group_watch"] = {"status": "skipped", "active_groups": 0, "groups": []}
        report["e2ee_sessions"] = {"active": 0}
        return report

    if report["local_database"]["status"] == "error":
        report["identity"] = {
            "status": "local_database_migration_failed",
            "did": None,
            "name": None,
            "jwt_valid": False,
            "error": "Local database migration failed",
        }
        report["inbox"] = {"status": "skipped", "total": 0}
        report["group_watch"] = {"status": "skipped", "active_groups": 0, "groups": []}
        report["e2ee_sessions"] = {"active": 0}
        return report

    # 1. Identity check
    report["identity"] = await check_identity(credential_name)

    # Return early if identity does not exist
    if report["identity"]["status"] == "no_identity":
        report["inbox"] = {"status": "skipped", "total": 0}
        report["group_watch"] = {"status": "no_identity", "active_groups": 0, "groups": []}
        report["e2ee_sessions"] = {"active": 0}
        return report

    # 2. Local discovery-group watch summary
    report["group_watch"] = summarize_group_watch(report["identity"].get("did"))

    # 3. Inbox summary
    report["inbox"] = await summarize_inbox(credential_name)

    # 4. E2EE auto-processing (optional)
    if auto_e2ee:
        report["e2ee_auto"] = await auto_process_e2ee(credential_name)
        # Refresh inbox so the report reflects the post-processing state.
        report["inbox"] = await summarize_inbox(credential_name)

    # 5. E2EE session status
    e2ee_state = load_e2ee_state(credential_name)
    if e2ee_state is not None:
        sessions = e2ee_state.get("sessions", [])
        active_count = len(sessions)
        report["e2ee_sessions"] = {"active": active_count}
    else:
        report["e2ee_sessions"] = {"active": 0}

    return report


def main() -> None:
    configure_logging(console_level=None, mirror_stdio=True)

    parser = argparse.ArgumentParser(description="Unified status check")
    parser.add_argument(
        "--upgrade-only",
        action="store_true",
        help="Run local skill upgrade checks/migrations and exit",
    )
    parser.add_argument(
        "--no-auto-e2ee",
        action="store_true",
        help="Disable automatic processing of E2EE protocol messages in inbox",
    )
    parser.add_argument(
        "--credential",
        type=str,
        default="default",
        help="Credential name (default: default)",
    )
    args = parser.parse_args()
    logging.getLogger(__name__).info(
        "check_status CLI started credential=%s auto_e2ee=%s upgrade_only=%s",
        args.credential,
        not args.no_auto_e2ee,
        args.upgrade_only,
    )

    if args.upgrade_only:
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "local_upgrade": ensure_local_upgrade_ready(args.credential),
        }
        report["credential_layout"] = report["local_upgrade"]["credential_layout"]
        report["local_database"] = report["local_upgrade"]["local_database"]
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    report = asyncio.run(check_status(args.credential, not args.no_auto_e2ee))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
