"""JSON-RPC 2.0 客户端辅助函数。

[INPUT]: httpx.AsyncClient, 端点路径, 方法名, 参数
[OUTPUT]: rpc_call() 辅助函数, JsonRpcError 异常类
[POS]: 为 auth.py 和外部调用者提供统一的 JSON-RPC 调用封装

[PROTOCOL]:
1. 逻辑变更时同步更新此头部
2. 更新后检查所在文件夹的 CLAUDE.md
"""

from __future__ import annotations

from typing import Any

import httpx


class JsonRpcError(Exception):
    """JSON-RPC 错误响应异常。"""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"JSON-RPC error {code}: {message}")


async def rpc_call(
    client: httpx.AsyncClient,
    endpoint: str,
    method: str,
    params: dict | None = None,
    request_id: int | str = 1,
) -> Any:
    """发送 JSON-RPC 2.0 请求并返回 result。

    Args:
        client: httpx 异步客户端。
        endpoint: RPC 端点路径（如 "/did-auth/rpc"）。
        method: RPC 方法名（如 "register"）。
        params: 方法参数。
        request_id: 请求 ID。

    Returns:
        JSON-RPC result 字段的值。

    Raises:
        JsonRpcError: 服务端返回 JSON-RPC error 时。
        httpx.HTTPStatusError: HTTP 层错误时。
    """
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": request_id,
    }
    resp = await client.post(endpoint, json=payload)
    resp.raise_for_status()
    body = resp.json()
    if body.get("error") is not None:
        error = body["error"]
        raise JsonRpcError(
            error["code"],
            error["message"],
            error.get("data"),
        )
    return body["result"]


__all__ = [
    "JsonRpcError",
    "rpc_call",
]
