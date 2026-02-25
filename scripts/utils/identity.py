"""DID 身份创建（封装 ANP）。

[INPUT]: hostname, path_segments, proof_purpose, domain, services
[OUTPUT]: DIDIdentity, create_identity(), load_private_key()
[POS]: 封装 ANP 的 create_did_wba_document()，提供 DID 身份创建能力

[PROTOCOL]:
1. 逻辑变更时同步更新此头部
2. 更新后检查所在文件夹的 CLAUDE.md
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from anp.authentication import create_did_wba_document


@dataclass
class DIDIdentity:
    """完整的 DID 身份信息。"""

    did: str
    did_document: dict[str, Any]  # 含 proof（由 ANP 生成）
    private_key_pem: bytes  # PEM 编码的 secp256k1 私钥
    public_key_pem: bytes  # PEM 编码的公钥
    user_id: str | None = field(default=None)  # 注册后填充
    jwt_token: str | None = field(default=None)  # WBA 认证后填充

    @property
    def unique_id(self) -> str:
        """从 DID 中提取 unique_id（最后一个路径段）。

        例如 did:wba:localhost:user:abc123 → abc123
        """
        return self.did.rsplit(":", 1)[-1]

    def get_private_key(self) -> ec.EllipticCurvePrivateKey:
        """从 PEM 加载 secp256k1 私钥对象。"""
        return load_private_key(self.private_key_pem)


def create_identity(
    hostname: str,
    path_segments: list[str] | None = None,
    proof_purpose: str = "authentication",
    domain: str | None = None,
    challenge: str | None = None,
    services: list[dict[str, Any]] | None = None,
) -> DIDIdentity:
    """创建 DID 身份（secp256k1 密钥对 + DID 文档 + proof）。

    使用 ANP 的 create_did_wba_document() 一次性生成完整的 DID 文档，
    包含密钥对和指定 purpose 的 W3C Data Integrity Proof。

    Args:
        hostname: DID 所属域名
        path_segments: DID 路径段，如 ["user", "alice"]
        proof_purpose: proof 用途（默认 "authentication"，用于注册）
        domain: proof 绑定的服务域名（服务端会验证）
        challenge: proof nonce（默认自动生成，用于防重放）
        services: 自定义 service 条目列表。每项为包含 "id", "type",
            "serviceEndpoint" 的 dict。如果 "id" 以 "#" 开头，
            会自动加上 DID 前缀。所有 service 在 proof 签名前写入文档。

    Returns:
        DIDIdentity（did_document 含 ANP 生成的 proof）
    """
    if challenge is None:
        challenge = secrets.token_hex(16)

    did_document, keys = create_did_wba_document(
        hostname=hostname,
        path_segments=path_segments,
        proof_purpose=proof_purpose,
        domain=domain,
        challenge=challenge,
        services=services,
    )

    private_key_pem, public_key_pem = keys["key-1"]

    return DIDIdentity(
        did=did_document["id"],
        did_document=did_document,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
    )


def load_private_key(pem_bytes: bytes) -> ec.EllipticCurvePrivateKey:
    """从 PEM 字节加载私钥。"""
    key = load_pem_private_key(pem_bytes, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TypeError(f"Expected EllipticCurvePrivateKey, got {type(key).__name__}")
    return key


__all__ = ["DIDIdentity", "create_identity", "load_private_key"]
