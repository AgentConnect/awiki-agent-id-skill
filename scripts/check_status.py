"""统一状态检查：身份验证 + 收件箱分类摘要 + E2EE 自动握手处理。

用法：
    python scripts/check_status.py                     # 基础状态检查
    python scripts/check_status.py --auto-e2ee         # 含 E2EE 自动处理
    python scripts/check_status.py --credential alice   # 指定凭证

[INPUT]: SDK（RPC 调用、E2eeClient）、credential_store、e2ee_store
[OUTPUT]: 结构化 JSON 状态报告（identity + inbox + e2ee_auto + e2ee_sessions）
[POS]: 统一状态检查入口，供 Agent 会话启动和心跳调用

[PROTOCOL]:
1. 逻辑变更时同步更新此头部
2. 更新后检查所在文件夹的 CLAUDE.md
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any

from utils import (
    SDKConfig,
    E2eeClient,
    create_user_service_client,
    create_molt_message_client,
    get_jwt_via_wba,
    rpc_call,
)
from utils.identity import DIDIdentity
from credential_store import load_identity, update_jwt
from e2ee_store import load_e2ee_state, save_e2ee_state


MESSAGE_RPC = "/message/rpc"
AUTH_RPC = "/user-service/did-auth/rpc"

# E2EE 协议消息类型
_E2EE_HANDSHAKE_TYPES = {"e2ee_hello", "e2ee_finished", "e2ee_error"}
_E2EE_MSG_TYPES = {"e2ee_hello", "e2ee_finished", "e2ee", "e2ee_error"}
_E2EE_TYPE_ORDER = {"e2ee_hello": 0, "e2ee_finished": 1, "e2ee": 2, "e2ee_error": 3}


# ---------- E2EE helpers（复制自 e2ee_messaging.py，避免修改现有文件） ----------

def _load_or_create_e2ee_client(
    local_did: str, credential_name: str
) -> E2eeClient:
    """从磁盘加载已有 E2EE 客户端状态，不存在时创建新客户端。"""
    state = load_e2ee_state(credential_name)
    if state is not None and state.get("local_did") == local_did:
        return E2eeClient.from_state(state)
    if state is not None and state.get("signing_pem"):
        return E2eeClient(local_did, signing_pem=state["signing_pem"])
    return E2eeClient(local_did)


def _save_e2ee_client(client: E2eeClient, credential_name: str) -> None:
    """将 E2EE 客户端状态保存到磁盘。"""
    save_e2ee_state(client.export_state(), credential_name)


async def _send_msg(http_client, sender_did, receiver_did, msg_type, content):
    """发送消息（E2EE 或普通消息）。"""
    if isinstance(content, dict):
        content = json.dumps(content)
    return await rpc_call(
        http_client, MESSAGE_RPC, "send",
        params={
            "sender_did": sender_did,
            "receiver_did": receiver_did,
            "content": content,
            "type": msg_type,
        },
    )


# ---------- 核心函数 ----------

async def check_identity(credential_name: str = "default") -> dict[str, Any]:
    """检查身份状态，自动刷新过期 JWT。"""
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
    async with create_user_service_client(config) as client:
        client.headers["Authorization"] = f"Bearer {data['jwt_token']}"
        try:
            await rpc_call(client, AUTH_RPC, "get_me")
            result["jwt_valid"] = True
        except Exception:
            # JWT 过期，尝试刷新
            try:
                private_key_pem = data["private_key_pem"]
                if isinstance(private_key_pem, str):
                    private_key_pem = private_key_pem.encode("utf-8")
                public_key_pem = data["public_key_pem"]
                if isinstance(public_key_pem, str):
                    public_key_pem = public_key_pem.encode("utf-8")

                identity = DIDIdentity(
                    did=data["did"],
                    did_document={},
                    private_key_pem=private_key_pem,
                    public_key_pem=public_key_pem,
                    user_id=data.get("user_id"),
                )
                new_token = await get_jwt_via_wba(client, identity, config.did_domain)
                update_jwt(credential_name, new_token)
                result["jwt_valid"] = True
                result["jwt_refreshed"] = True
            except Exception as e:
                result["status"] = "jwt_refresh_failed"
                result["error"] = str(e)

    return result


async def summarize_inbox(
    credential_name: str = "default",
) -> dict[str, Any]:
    """获取收件箱并分类统计。"""
    data = load_identity(credential_name)
    if data is None:
        return {"status": "no_identity", "total": 0}

    config = SDKConfig()
    try:
        async with create_molt_message_client(config) as client:
            client.headers["Authorization"] = f"Bearer {data['jwt_token']}"
            inbox = await rpc_call(
                client, MESSAGE_RPC, "get_inbox",
                params={"user_did": data["did"], "limit": 50},
            )
    except Exception as e:
        return {"status": "error", "error": str(e), "total": 0}

    messages = inbox.get("messages", [])

    # 按类型统计
    by_type: dict[str, int] = {}
    text_by_sender: dict[str, dict[str, Any]] = {}
    e2ee_handshake_pending: list[str] = []
    e2ee_encrypted_from: list[str] = []
    text_count = 0

    for msg in messages:
        msg_type = msg.get("type", "unknown")
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
        elif msg_type == "e2ee_hello":
            if sender_did not in e2ee_handshake_pending:
                e2ee_handshake_pending.append(sender_did)
        elif msg_type == "e2ee":
            if sender_did not in e2ee_encrypted_from:
                e2ee_encrypted_from.append(sender_did)

    return {
        "status": "ok",
        "total": len(messages),
        "by_type": by_type,
        "text_messages": text_count,
        "text_by_sender": text_by_sender,
        "e2ee_handshake_pending": e2ee_handshake_pending,
        "e2ee_encrypted_from": e2ee_encrypted_from,
        "has_pending_handshakes": len(e2ee_handshake_pending) > 0,
    }


async def auto_process_e2ee(
    credential_name: str = "default",
) -> dict[str, Any]:
    """自动处理收件箱中的 E2EE 握手消息。

    关键改进：响应路由到 msg.sender_did，支持来自任意 peer 的握手。
    """
    data = load_identity(credential_name)
    if data is None:
        return {"status": "no_identity", "processed": 0, "details": []}

    config = SDKConfig()
    try:
        async with create_molt_message_client(config) as client:
            client.headers["Authorization"] = f"Bearer {data['jwt_token']}"

            # 获取收件箱
            inbox = await rpc_call(
                client, MESSAGE_RPC, "get_inbox",
                params={"user_did": data["did"], "limit": 50},
            )
            messages = inbox.get("messages", [])

            # 筛选 E2EE 协议消息（不含加密消息本身）
            e2ee_msgs = [
                m for m in messages
                if m.get("type") in _E2EE_HANDSHAKE_TYPES
            ]

            if not e2ee_msgs:
                return {"status": "ok", "processed": 0, "details": []}

            # 按时间 + 协议顺序排序
            e2ee_msgs.sort(key=lambda m: (
                m.get("created_at", ""),
                _E2EE_TYPE_ORDER.get(m.get("type"), 99),
            ))

            e2ee_client = _load_or_create_e2ee_client(data["did"], credential_name)
            details: list[dict[str, Any]] = []
            processed_ids: list[str] = []

            for msg in e2ee_msgs:
                msg_type = msg["type"]
                sender_did = msg.get("sender_did", "")
                content = json.loads(msg["content"]) if isinstance(msg.get("content"), str) else msg.get("content", {})

                try:
                    responses = e2ee_client.process_e2ee_message(msg_type, content)
                    # 响应路由到 sender_did（而非固定 peer）
                    for resp_type, resp_content in responses:
                        await _send_msg(
                            client, data["did"], sender_did, resp_type, resp_content
                        )

                    details.append({
                        "msg_type": msg_type,
                        "sender_did": sender_did,
                        "responses_sent": len(responses),
                    })
                    processed_ids.append(msg["id"])
                except Exception as e:
                    details.append({
                        "msg_type": msg_type,
                        "sender_did": sender_did,
                        "error": str(e),
                    })

            # 标记已处理的消息为已读
            if processed_ids:
                await rpc_call(
                    client, MESSAGE_RPC, "mark_read",
                    params={"user_did": data["did"], "message_ids": processed_ids},
                )

            # 保存 E2EE 状态
            _save_e2ee_client(e2ee_client, credential_name)

            return {
                "status": "ok",
                "processed": len(processed_ids),
                "details": details,
            }

    except Exception as e:
        return {"status": "error", "processed": 0, "details": [], "error": str(e)}


async def check_status(
    credential_name: str = "default",
    auto_e2ee: bool = False,
) -> dict[str, Any]:
    """统一状态检查编排器。"""
    report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # 1. 身份检查
    report["identity"] = await check_identity(credential_name)

    # 身份不存在时提前返回
    if report["identity"]["status"] == "no_identity":
        report["inbox"] = {"status": "skipped", "total": 0}
        report["e2ee_sessions"] = {"active": 0, "pending": 0}
        return report

    # 2. 收件箱摘要
    report["inbox"] = await summarize_inbox(credential_name)

    # 3. E2EE 自动处理（可选）
    if auto_e2ee:
        report["e2ee_auto"] = await auto_process_e2ee(credential_name)

    # 4. E2EE 会话状态
    e2ee_state = load_e2ee_state(credential_name)
    if e2ee_state is not None:
        sessions = e2ee_state.get("sessions", [])
        pending_sessions = e2ee_state.get("pending_sessions", [])
        active_count = len(sessions)
        pending_count = len(pending_sessions)
        report["e2ee_sessions"] = {"active": active_count, "pending": pending_count}
    else:
        report["e2ee_sessions"] = {"active": 0, "pending": 0}

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="统一状态检查")
    parser.add_argument(
        "--auto-e2ee", action="store_true",
        help="自动处理收件箱中的 E2EE 握手消息",
    )
    parser.add_argument(
        "--credential", type=str, default="default",
        help="凭证名称（默认: default）",
    )
    args = parser.parse_args()

    report = asyncio.run(check_status(args.credential, args.auto_e2ee))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
