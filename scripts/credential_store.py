"""凭证持久化：保存/加载私钥、DID、JWT 到本地文件。

[INPUT]: DIDIdentity 对象
[OUTPUT]: save_identity(), load_identity(), list_identities(), delete_identity()
[POS]: 凭证管理核心模块，支持跨会话身份复用

[PROTOCOL]:
1. 逻辑变更时同步更新此头部
2. 更新后检查所在文件夹的 CLAUDE.md
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 凭证存储目录（相对于 SKILL_DIR）
_CREDENTIALS_DIR = Path(__file__).resolve().parent.parent / ".credentials"


def _ensure_credentials_dir() -> Path:
    """确保凭证目录存在并设置权限。"""
    _CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    # 目录权限设为 700（仅当前用户可访问）
    os.chmod(_CREDENTIALS_DIR, stat.S_IRWXU)
    return _CREDENTIALS_DIR


def _credential_path(name: str) -> Path:
    """获取凭证文件路径。"""
    return _ensure_credentials_dir() / f"{name}.json"


def save_identity(
    did: str,
    unique_id: str,
    user_id: str | None,
    private_key_pem: bytes,
    public_key_pem: bytes,
    jwt_token: str | None = None,
    display_name: str | None = None,
    name: str = "default",
) -> Path:
    """保存 DID 身份到本地文件。

    Args:
        did: DID 标识符
        unique_id: 从 DID 提取的唯一 ID
        user_id: 注册后的用户 ID
        private_key_pem: PEM 编码的私钥
        public_key_pem: PEM 编码的公钥
        jwt_token: JWT token
        display_name: 显示名称
        name: 凭证名称（默认 "default"）

    Returns:
        凭证文件路径
    """
    credential_data: dict[str, Any] = {
        "did": did,
        "unique_id": unique_id,
        "user_id": user_id,
        "private_key_pem": private_key_pem.decode("utf-8")
            if isinstance(private_key_pem, bytes) else private_key_pem,
        "public_key_pem": public_key_pem.decode("utf-8")
            if isinstance(public_key_pem, bytes) else public_key_pem,
        "jwt_token": jwt_token,
        "name": display_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    path = _credential_path(name)
    path.write_text(json.dumps(credential_data, indent=2, ensure_ascii=False))
    # 私钥文件权限设为 600（仅当前用户可读写）
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def load_identity(name: str = "default") -> dict[str, Any] | None:
    """从本地文件加载 DID 身份。

    Args:
        name: 凭证名称（默认 "default"）

    Returns:
        凭证数据字典，不存在时返回 None
    """
    path = _credential_path(name)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return data


def list_identities() -> list[dict[str, Any]]:
    """列出所有已保存的身份。

    Returns:
        身份列表，每项含 name、did、created_at 等信息
    """
    cred_dir = _ensure_credentials_dir()
    identities = []
    for path in sorted(cred_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            identities.append({
                "credential_name": path.stem,
                "did": data.get("did", ""),
                "unique_id": data.get("unique_id", ""),
                "name": data.get("name", ""),
                "user_id": data.get("user_id", ""),
                "created_at": data.get("created_at", ""),
                "has_jwt": bool(data.get("jwt_token")),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return identities


def delete_identity(name: str) -> bool:
    """删除已保存的身份。

    Args:
        name: 凭证名称

    Returns:
        是否成功删除
    """
    path = _credential_path(name)
    if path.exists():
        path.unlink()
        return True
    return False


def update_jwt(name: str, jwt_token: str) -> bool:
    """更新已保存身份的 JWT token。

    Args:
        name: 凭证名称
        jwt_token: 新的 JWT token

    Returns:
        是否成功更新
    """
    data = load_identity(name)
    if data is None:
        return False
    data["jwt_token"] = jwt_token
    path = _credential_path(name)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return True
