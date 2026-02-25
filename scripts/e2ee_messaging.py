"""E2EE 端到端加密消息收发（支持跨进程状态持久化）。

用法：
    # 发起 E2EE 握手
    uv run python scripts/e2ee_messaging.py --handshake "did:wba:awiki.info:user:abc123"

    # 发送加密消息（需要先完成握手）
    uv run python scripts/e2ee_messaging.py --send "did:wba:awiki.info:user:abc123" --content "secret message"

    # 处理收件箱中的 E2EE 消息（握手响应 + 解密）
    uv run python scripts/e2ee_messaging.py --process --peer "did:wba:awiki.info:user:abc123"

支持的工作流：
1. Alice: --handshake <Bob's DID>       → 发起握手
2. Bob:   --process --peer <Alice's DID> → 处理收件箱（自动创建 E2eeClient + 回复握手）
3. Alice: --process --peer <Bob's DID>   → 处理收件箱（完成握手，状态自动持久化）
4. Bob:   --process --peer <Alice's DID> → 处理收件箱（激活会话，状态自动持久化）
5. Alice: --send <Bob's DID> --content "secret" → 从磁盘恢复会话，发送加密消息
6. Bob:   --process --peer <Alice's DID> → 从磁盘恢复会话，解密消息

[INPUT]: SDK（E2eeClient、RPC 调用）、credential_store（加载身份凭证）、e2ee_store（E2EE 状态持久化）
[OUTPUT]: E2EE 操作结果
[POS]: 端到端加密消息脚本，集成状态持久化支持跨进程 E2EE 通信

[PROTOCOL]:
1. 逻辑变更时同步更新此头部
2. 更新后检查所在文件夹的 CLAUDE.md
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from utils import SDKConfig, E2eeClient, create_molt_message_client, rpc_call
from credential_store import load_identity
from e2ee_store import save_e2ee_state, load_e2ee_state


MESSAGE_RPC = "/message/rpc"

# E2EE 相关消息类型
_E2EE_MSG_TYPES = {"e2ee_hello", "e2ee_finished", "e2ee", "e2ee_error"}

# E2EE 消息类型的协议顺序
_E2EE_TYPE_ORDER = {"e2ee_hello": 0, "e2ee_finished": 1, "e2ee": 2, "e2ee_error": 3}


def _load_or_create_e2ee_client(
    local_did: str, credential_name: str
) -> E2eeClient:
    """从磁盘加载已有 E2EE 客户端状态，不存在时创建新客户端。"""
    state = load_e2ee_state(credential_name)
    if state is not None and state.get("local_did") == local_did:
        client = E2eeClient.from_state(state)
        return client
    # 尝试复用已有的 signing_pem
    if state is not None and state.get("signing_pem"):
        return E2eeClient(local_did, signing_pem=state["signing_pem"])
    return E2eeClient(local_did)


def _save_e2ee_client(client: E2eeClient, credential_name: str) -> None:
    """将 E2EE 客户端状态保存到磁盘。"""
    state = client.export_state()
    save_e2ee_state(state, credential_name)


async def _send_msg(client, sender_did, receiver_did, msg_type, content):
    """发送消息（E2EE 或普通消息）。"""
    if isinstance(content, dict):
        content = json.dumps(content)
    return await rpc_call(
        client, MESSAGE_RPC, "send",
        params={
            "sender_did": sender_did,
            "receiver_did": receiver_did,
            "content": content,
            "type": msg_type,
        },
    )


async def initiate_handshake(
    peer_did: str,
    credential_name: str = "default",
) -> None:
    """发起 E2EE 握手。"""
    data = load_identity(credential_name)
    if data is None:
        print(f"未找到凭证 '{credential_name}'，请先创建身份")
        sys.exit(1)

    e2ee_client = _load_or_create_e2ee_client(data["did"], credential_name)
    msg_type, content = e2ee_client.initiate_handshake(peer_did)

    config = SDKConfig()
    async with create_molt_message_client(config) as client:
        client.headers["Authorization"] = f"Bearer {data['jwt_token']}"
        await _send_msg(client, data["did"], peer_did, msg_type, content)

    _save_e2ee_client(e2ee_client, credential_name)

    print(f"E2EE 握手已发起")
    print(f"  session_id: {content.get('session_id')}")
    print(f"  peer_did  : {peer_did}")
    print("接下来等待对方处理收件箱并响应握手")


async def send_encrypted(
    peer_did: str,
    plaintext: str,
    credential_name: str = "default",
) -> None:
    """发送加密消息。"""
    data = load_identity(credential_name)
    if data is None:
        print(f"未找到凭证 '{credential_name}'，请先创建身份")
        sys.exit(1)

    e2ee_client = _load_or_create_e2ee_client(data["did"], credential_name)

    if not e2ee_client.has_active_session(peer_did):
        print(f"没有与 {peer_did} 的活跃 E2EE 会话")
        print("请先完成握手流程")
        sys.exit(1)

    enc_type, enc_content = e2ee_client.encrypt_message(peer_did, plaintext)

    config = SDKConfig()
    async with create_molt_message_client(config) as client:
        client.headers["Authorization"] = f"Bearer {data['jwt_token']}"
        await _send_msg(client, data["did"], peer_did, enc_type, enc_content)

    print("加密消息已发送")
    print(f"  原文: {plaintext}")
    print(f"  接收方: {peer_did}")


async def process_inbox(
    peer_did: str,
    credential_name: str = "default",
) -> None:
    """处理收件箱中的 E2EE 消息。"""
    data = load_identity(credential_name)
    if data is None:
        print(f"未找到凭证 '{credential_name}'，请先创建身份")
        sys.exit(1)

    config = SDKConfig()
    async with create_molt_message_client(config) as client:
        client.headers["Authorization"] = f"Bearer {data['jwt_token']}"

        # 获取收件箱
        inbox = await rpc_call(
            client, MESSAGE_RPC, "get_inbox",
            params={"user_did": data["did"], "limit": 50},
        )
        messages = inbox.get("messages", [])
        if not messages:
            print("收件箱为空")
            return

        # 按时间和协议顺序排序
        messages.sort(key=lambda m: (
            m.get("created_at", ""),
            _E2EE_TYPE_ORDER.get(m.get("type"), 99),
        ))

        e2ee_client: E2eeClient | None = None

        # 尝试从磁盘恢复已有 E2EE 客户端
        saved_state = load_e2ee_state(credential_name)
        if saved_state is not None and saved_state.get("local_did") == data["did"]:
            e2ee_client = E2eeClient.from_state(saved_state)
        processed_ids = []

        for msg in messages:
            msg_type = msg["type"]
            sender_did = msg.get("sender_did", "?")

            if msg_type in _E2EE_MSG_TYPES:
                content = json.loads(msg["content"])

                if msg_type == "e2ee_hello" and e2ee_client is None:
                    print(f"  [{msg_type}] 收到 E2EE 协商请求，创建 E2eeClient...")
                    # 尝试复用已保存的 signing_pem
                    if saved_state is not None and saved_state.get("signing_pem"):
                        e2ee_client = E2eeClient(
                            data["did"], signing_pem=saved_state["signing_pem"]
                        )
                    else:
                        e2ee_client = E2eeClient(data["did"])

                if e2ee_client is None:
                    print(f"  [{msg_type}] 尚无 E2eeClient，跳过")
                    continue

                if msg_type == "e2ee":
                    original_type, plaintext = e2ee_client.decrypt_message(content)
                    print(f"  [{msg_type}] 解密消息: [{original_type}] {plaintext}")
                else:
                    responses = e2ee_client.process_e2ee_message(msg_type, content)
                    print(f"  [{msg_type}] 处理协议消息，生成 {len(responses)} 条响应")
                    for resp_type, resp_content in responses:
                        await _send_msg(
                            client, data["did"], peer_did, resp_type, resp_content
                        )
                        print(f"    -> 发送 {resp_type}")
            else:
                print(f"  [{msg_type}] 来自 {sender_did[:40]}...: {msg['content']}")

            processed_ids.append(msg["id"])

        # 标记已读
        if processed_ids:
            await rpc_call(
                client, MESSAGE_RPC, "mark_read",
                params={"user_did": data["did"], "message_ids": processed_ids},
            )
            print(f"\n已标记 {len(processed_ids)} 条消息为已读")

        if e2ee_client and e2ee_client.has_active_session(peer_did):
            print(f"\nE2EE 会话状态: ACTIVE (与 {peer_did})")
        elif e2ee_client:
            print(f"\nE2EE 会话状态: 握手进行中")

        # 保存 E2EE 客户端状态到磁盘（签名密钥 + ACTIVE 会话）
        if e2ee_client is not None:
            _save_e2ee_client(e2ee_client, credential_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="E2EE 端到端加密消息")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--handshake", type=str, help="向指定 DID 发起 E2EE 握手")
    group.add_argument("--send", type=str, help="向指定 DID 发送加密消息")
    group.add_argument("--process", action="store_true",
                       help="处理收件箱中的 E2EE 消息")

    parser.add_argument("--content", type=str, help="消息内容（--send 时必需）")
    parser.add_argument("--peer", type=str,
                        help="对端 DID（--process 时必需）")
    parser.add_argument("--credential", type=str, default="default",
                        help="凭证名称（默认: default）")

    args = parser.parse_args()

    if args.handshake:
        asyncio.run(initiate_handshake(args.handshake, args.credential))
    elif args.send:
        if not args.content:
            parser.error("发送加密消息需要 --content")
        asyncio.run(send_encrypted(args.send, args.content, args.credential))
    elif args.process:
        if not args.peer:
            parser.error("处理收件箱需要 --peer")
        asyncio.run(process_inbox(args.peer, args.credential))


if __name__ == "__main__":
    main()
