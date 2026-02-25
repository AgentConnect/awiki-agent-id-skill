"""检查收件箱、查看聊天历史、标记已读。

用法：
    # 查看收件箱
    uv run python scripts/check_inbox.py

    # 限制返回数量
    uv run python scripts/check_inbox.py --limit 5

    # 查看与指定 DID 的聊天历史
    uv run python scripts/check_inbox.py --history "did:wba:localhost:user:abc123"

    # 标记消息为已读
    uv run python scripts/check_inbox.py --mark-read msg_id_1 msg_id_2

[INPUT]: SDK（RPC 调用）、credential_store（加载身份凭证）
[OUTPUT]: 收件箱消息列表 / 聊天历史 / 标记已读结果
[POS]: 消息接收与处理脚本

[PROTOCOL]:
1. 逻辑变更时同步更新此头部
2. 更新后检查所在文件夹的 CLAUDE.md
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from utils import SDKConfig, create_molt_message_client, rpc_call
from credential_store import load_identity


MESSAGE_RPC = "/message/rpc"


async def check_inbox(credential_name: str = "default", limit: int = 20) -> None:
    """查看收件箱。"""
    data = load_identity(credential_name)
    if data is None:
        print(f"未找到凭证 '{credential_name}'，请先创建身份")
        sys.exit(1)

    config = SDKConfig()
    async with create_molt_message_client(config) as client:
        client.headers["Authorization"] = f"Bearer {data['jwt_token']}"
        inbox = await rpc_call(
            client,
            MESSAGE_RPC,
            "get_inbox",
            params={"user_did": data["did"], "limit": limit},
        )
        print(json.dumps(inbox, indent=2, ensure_ascii=False))


async def get_history(
    peer_did: str,
    credential_name: str = "default",
    limit: int = 50,
) -> None:
    """查看与指定 DID 的聊天历史。"""
    data = load_identity(credential_name)
    if data is None:
        print(f"未找到凭证 '{credential_name}'，请先创建身份")
        sys.exit(1)

    config = SDKConfig()
    async with create_molt_message_client(config) as client:
        client.headers["Authorization"] = f"Bearer {data['jwt_token']}"
        history = await rpc_call(
            client,
            MESSAGE_RPC,
            "get_history",
            params={
                "user_did": data["did"],
                "peer_did": peer_did,
                "limit": limit,
            },
        )
        print(json.dumps(history, indent=2, ensure_ascii=False))


async def mark_read(
    message_ids: list[str],
    credential_name: str = "default",
) -> None:
    """标记消息为已读。"""
    data = load_identity(credential_name)
    if data is None:
        print(f"未找到凭证 '{credential_name}'，请先创建身份")
        sys.exit(1)

    config = SDKConfig()
    async with create_molt_message_client(config) as client:
        client.headers["Authorization"] = f"Bearer {data['jwt_token']}"
        result = await rpc_call(
            client,
            MESSAGE_RPC,
            "mark_read",
            params={
                "user_did": data["did"],
                "message_ids": message_ids,
            },
        )
        print("标记已读成功:")
        print(json.dumps(result, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="检查收件箱和消息管理")
    parser.add_argument("--history", type=str, help="查看与指定 DID 的聊天历史")
    parser.add_argument("--mark-read", nargs="+", type=str,
                        help="标记指定消息 ID 为已读")
    parser.add_argument("--limit", type=int, default=20,
                        help="返回数量限制（默认: 20）")
    parser.add_argument("--credential", type=str, default="default",
                        help="凭证名称（默认: default）")

    args = parser.parse_args()

    if args.mark_read:
        asyncio.run(mark_read(args.mark_read, args.credential))
    elif args.history:
        asyncio.run(get_history(args.history, args.credential, args.limit))
    else:
        asyncio.run(check_inbox(args.credential, args.limit))


if __name__ == "__main__":
    main()
