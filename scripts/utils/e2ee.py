"""E2EE 端到端加密客户端（封装 ANP e2e_encryption_v2）。

[INPUT]: ANP E2eeSession / E2eeKeyManager / detect_message_type, local_did
[OUTPUT]: E2eeClient 类，提供握手、加密、解密、状态导出/恢复的高层 API
[POS]: 封装 ANP 底层 E2EE 协议，为上层应用提供简洁的加解密接口；支持跨进程状态持久化

[PROTOCOL]:
1. 逻辑变更时同步更新此头部
2. 更新后检查所在文件夹的 CLAUDE.md
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)

from anp.e2e_encryption_v2 import (
    E2eeKeyManager,
    E2eeSession,
    SessionState,
    detect_message_type,
)

logger = logging.getLogger(__name__)


def _generate_secp256r1_pem() -> tuple[str, ec.EllipticCurvePrivateKey]:
    """生成 secp256r1 密钥对，返回 (pem_str, private_key_obj)。"""
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem_bytes = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    return pem_bytes.decode("utf-8"), private_key


def _load_secp256r1_pem(pem_str: str) -> tuple[str, ec.EllipticCurvePrivateKey]:
    """从 PEM 字符串加载 secp256r1 密钥，返回 (pem_str, private_key_obj)。"""
    private_key = load_pem_private_key(pem_str.encode("utf-8"), password=None)
    return pem_str, private_key


class E2eeClient:
    """E2EE 端到端加密客户端。

    封装 ANP ``E2eeSession`` 和 ``E2eeKeyManager``，提供：
    - 握手发起与协议消息处理
    - 消息加密与解密
    - 过期会话清理

    关键设计：E2EE 协议使用 secp256r1 曲线（ECDHE 临时密钥 + proof 签名密钥），
    与 DID 身份密钥（secp256k1）分离。构造函数自动生成独立的 secp256r1 签名密钥。
    """

    def __init__(self, local_did: str, *, signing_pem: str | None = None) -> None:
        """初始化 E2EE 客户端。

        Args:
            local_did: 本地 DID 标识符。
            signing_pem: 可选的 secp256r1 签名密钥 PEM 字符串。
                传入时复用该密钥，不传则自动生成新密钥。
        """
        self.local_did = local_did
        if signing_pem is not None:
            self._signing_pem, self._signing_key = _load_secp256r1_pem(signing_pem)
        else:
            self._signing_pem, self._signing_key = _generate_secp256r1_pem()
        self._key_manager = E2eeKeyManager()

    def initiate_handshake(self, peer_did: str) -> tuple[str, dict[str, Any]]:
        """发起 E2EE 握手。

        创建新的 ``E2eeSession``，生成 SourceHello 消息。

        Args:
            peer_did: 对端 DID 标识符。

        Returns:
            ``(msg_type, content_dict)`` 元组，调用方负责序列化后通过 RPC 发送。
            msg_type 为 ``"e2ee_hello"``。
        """
        session = E2eeSession(
            local_did=self.local_did,
            did_private_key_pem=self._signing_pem,
            peer_did=peer_did,
        )
        msg_type, content = session.initiate_handshake()
        self._key_manager.register_pending_session(session)
        return msg_type, content

    def process_e2ee_message(
        self, msg_type: str, content: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """处理收到的 E2EE 协议消息。

        根据消息类型执行对应操作，返回需要发送给对端的响应消息列表。

        Args:
            msg_type: 消息类型（``e2ee_hello`` / ``e2ee_finished`` / ``e2ee_error``）。
            content: 消息内容 dict。

        Returns:
            需要发送的消息列表，每项为 ``(msg_type, content_dict)``。
            可能 0~2 条：

            - source_hello → 返回 ``[dest_hello, finished]``
            - destination_hello → 返回 ``[finished]``
            - finished → 返回 ``[]``（会话激活）
            - error → 返回 ``[]``
        """
        detected = detect_message_type(msg_type, content)
        if detected is None:
            logger.warning("无法识别的 E2EE 消息类型: %s", msg_type)
            return []

        if detected == "source_hello":
            return self._handle_source_hello(content)
        elif detected == "destination_hello":
            return self._handle_destination_hello(content)
        elif detected == "finished":
            return self._handle_finished(content)
        elif detected == "error":
            return self._handle_error(content)
        elif detected == "encrypted":
            logger.warning("process_e2ee_message 不处理加密消息，请使用 decrypt_message")
            return []
        else:
            logger.warning("未处理的 E2EE 消息子类型: %s", detected)
            return []

    def has_active_session(self, peer_did: str) -> bool:
        """检查是否存在与指定对端的活跃加密会话。

        Args:
            peer_did: 对端 DID 标识符。

        Returns:
            存在活跃（ACTIVE 且未过期）会话时返回 ``True``。
        """
        session = self._key_manager.get_active_session(self.local_did, peer_did)
        return session is not None

    def encrypt_message(
        self, peer_did: str, plaintext: str, original_type: str = "text"
    ) -> tuple[str, dict[str, Any]]:
        """加密消息。

        Args:
            peer_did: 对端 DID 标识符。
            plaintext: 明文内容。
            original_type: 原始消息类型（默认 ``"text"``）。

        Returns:
            ``(msg_type, content_dict)`` 元组，msg_type 为 ``"e2ee"``。

        Raises:
            RuntimeError: 没有与对端的活跃会话。
        """
        session = self._key_manager.get_active_session(self.local_did, peer_did)
        if session is None:
            raise RuntimeError(f"没有与 {peer_did} 的活跃 E2EE 会话")
        return session.encrypt_message(original_type, plaintext)

    def decrypt_message(self, content: dict[str, Any]) -> tuple[str, str]:
        """解密消息。

        根据 ``secret_key_id`` 查找对应的会话并解密。

        Args:
            content: 加密消息的 content dict（含 ``secret_key_id``、``encrypted`` 等）。

        Returns:
            ``(original_type, plaintext)`` 元组。

        Raises:
            RuntimeError: 找不到对应的会话。
        """
        secret_key_id = content.get("secret_key_id")
        if not secret_key_id:
            raise RuntimeError("消息缺少 secret_key_id")

        session = self._key_manager.get_session_by_key_id(secret_key_id)
        if session is None:
            raise RuntimeError(f"找不到 secret_key_id={secret_key_id} 对应的会话")
        return session.decrypt_message(content)

    def cleanup_expired(self) -> list[tuple[str, str]]:
        """清理过期会话。

        Returns:
            需要重新握手的 ``(local_did, peer_did)`` 列表。
        """
        return self._key_manager.cleanup_expired()

    # ------------------------------------------------------------------
    # 状态导出 / 恢复
    # ------------------------------------------------------------------

    def export_state(self) -> dict[str, Any]:
        """导出客户端状态（signing_pem + ACTIVE 会话 + PENDING 握手会话）。

        Returns:
            可 JSON 序列化的 dict，用于持久化。
        """
        sessions: list[dict[str, Any]] = []
        # 遍历 key_manager 内部的 did_pair 索引，收集 ACTIVE 且未过期的会话
        for session_list in self._key_manager._sessions_by_did_pair.values():
            for session in session_list:
                if session.state == SessionState.ACTIVE and not session.is_expired():
                    exported = self._export_session(session)
                    if exported is not None:
                        sessions.append(exported)
        # 收集 PENDING（握手中）的会话
        pending_sessions: list[dict[str, Any]] = []
        for session in self._key_manager._pending_sessions.values():
            exported = self._export_pending_session(session)
            if exported is not None:
                pending_sessions.append(exported)
        return {
            "local_did": self.local_did,
            "signing_pem": self._signing_pem,
            "sessions": sessions,
            "pending_sessions": pending_sessions,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> E2eeClient:
        """从导出的 dict 恢复完整客户端。

        Args:
            state: 由 ``export_state()`` 生成的 dict。

        Returns:
            恢复后的 ``E2eeClient`` 实例（ACTIVE 会话已注册到 key_manager）。
        """
        client = cls(state["local_did"], signing_pem=state["signing_pem"])
        for session_data in state.get("sessions", []):
            session = cls._restore_session(session_data)
            if session is not None:
                client._key_manager.register_session(session)
        # 恢复 PENDING 握手会话
        for session_data in state.get("pending_sessions", []):
            session = cls._restore_pending_session(session_data)
            if session is not None:
                client._key_manager.register_pending_session(session)
        return client

    @staticmethod
    def _export_session(session: E2eeSession) -> dict[str, Any] | None:
        """序列化单个 ACTIVE 会话。

        send_key / recv_key 使用 base64 编码。
        返回 None 表示会话不可导出（非 ACTIVE 或缺少关键数据）。
        """
        if session.state != SessionState.ACTIVE:
            return None
        send_key = session.send_key
        recv_key = session.recv_key
        if send_key is None or recv_key is None:
            return None
        return {
            "session_id": session.session_id,
            "local_did": session.local_did,
            "peer_did": session.peer_did,
            "is_initiator": session._is_initiator,
            "send_key": base64.b64encode(send_key).decode("ascii"),
            "recv_key": base64.b64encode(recv_key).decode("ascii"),
            "secret_key_id": session.secret_key_id,
            "cipher_suite": session.cipher_suite,
            "key_expires": session._key_expires,
            "created_at": session._created_at,
            "active_at": session._active_at,
        }

    @staticmethod
    def _export_pending_session(session: E2eeSession) -> dict[str, Any] | None:
        """序列化单个 PENDING（握手中）会话。

        包含临时 ECDHE 密钥和握手状态，支持跨进程握手恢复。
        返回 None 表示会话不可导出。
        """
        state = session.state
        if state not in (
            SessionState.HANDSHAKE_INITIATED,
            SessionState.HANDSHAKE_COMPLETING,
        ):
            return None

        # 检查握手超时（5 分钟）
        if time.time() - session._created_at > 300:
            return None

        # 序列化临时 ECDHE 私钥
        eph_pem = None
        if session._eph_private_key is not None:
            eph_pem = session._eph_private_key.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
            ).decode("utf-8")

        # 序列化 DID 签名私钥
        did_pem = None
        if session._did_private_key is not None:
            did_pem = session._did_private_key.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
            ).decode("utf-8")

        data: dict[str, Any] = {
            "session_id": session.session_id,
            "local_did": session.local_did,
            "peer_did": session.peer_did,
            "is_initiator": session._is_initiator,
            "state": state.value,
            "eph_private_key_pem": eph_pem,
            "eph_public_key_hex": session._eph_public_key_hex,
            "did_private_key_pem": did_pem,
            "did_public_key_hex": session._did_public_key_hex,
            "local_key_share": session._local_key_share,
            "local_random": session._local_random,
            "peer_random": session._peer_random,
            "created_at": session._created_at,
            "default_expires": session.default_expires,
        }

        # HANDSHAKE_COMPLETING 状态已派生加密密钥
        if state == SessionState.HANDSHAKE_COMPLETING:
            send_key = session.send_key
            recv_key = session.recv_key
            if send_key is not None:
                data["send_key"] = base64.b64encode(send_key).decode("ascii")
            if recv_key is not None:
                data["recv_key"] = base64.b64encode(recv_key).decode("ascii")
            data["cipher_suite"] = session._cipher_suite
            data["key_expires"] = session._key_expires

        return data

    @staticmethod
    def _restore_pending_session(data: dict[str, Any]) -> E2eeSession | None:
        """从 dict 恢复单个 PENDING 握手会话。

        Returns:
            恢复的 ``E2eeSession``，若已超时则返回 None。
        """
        created_at = data.get("created_at", 0)
        if time.time() - created_at > 300:
            return None

        state_str = data.get("state", "")
        try:
            state = SessionState(state_str)
        except ValueError:
            return None

        session = object.__new__(E2eeSession)
        session.local_did = data["local_did"]
        session.peer_did = data["peer_did"]
        session.session_id = data["session_id"]
        session.default_expires = data.get("default_expires", 86400)
        session._state = state
        session._is_initiator = data.get("is_initiator")
        session._created_at = created_at
        session._active_at = None
        session._local_random = data.get("local_random", "")
        session._peer_random = data.get("peer_random")
        session._local_key_share = data.get("local_key_share", {})
        session._did_public_key_hex = data.get("did_public_key_hex", "")

        # 恢复 DID 签名密钥
        did_pem = data.get("did_private_key_pem")
        if did_pem:
            session._did_private_key = load_pem_private_key(
                did_pem.encode("utf-8"), password=None
            )
        else:
            session._did_private_key = None

        # 恢复临时 ECDHE 密钥
        eph_pem = data.get("eph_private_key_pem")
        if eph_pem:
            session._eph_private_key = load_pem_private_key(
                eph_pem.encode("utf-8"), password=None
            )
            session._eph_public_key = session._eph_private_key.public_key()
        else:
            session._eph_private_key = None
            session._eph_public_key = None
        session._eph_public_key_hex = data.get("eph_public_key_hex", "")

        # 恢复加密密钥（HANDSHAKE_COMPLETING 状态）
        if "send_key" in data:
            session._send_key = base64.b64decode(data["send_key"])
        else:
            session._send_key = None
        if "recv_key" in data:
            session._recv_key = base64.b64decode(data["recv_key"])
        else:
            session._recv_key = None
        session._secret_key_id = data.get("secret_key_id")
        session._cipher_suite = data.get("cipher_suite")
        session._key_expires = data.get("key_expires")

        return session

    @staticmethod
    def _restore_session(data: dict[str, Any]) -> E2eeSession | None:
        """从 dict 恢复单个 ACTIVE 会话。

        使用 ``object.__new__(E2eeSession)`` 绕过 ``__init__``（避免重新生成密钥），
        直接设置恢复所需的属性。

        Returns:
            恢复的 ``E2eeSession``，若已过期则返回 None。
        """
        # 跳过已过期的会话
        active_at = data.get("active_at")
        key_expires = data.get("key_expires")
        if active_at is not None and key_expires is not None:
            if time.time() > active_at + key_expires:
                return None

        session = object.__new__(E2eeSession)
        # 公共属性
        session.local_did = data["local_did"]
        session.peer_did = data["peer_did"]
        session.session_id = data["session_id"]
        session.default_expires = key_expires or 86400
        # 状态与角色
        session._state = SessionState.ACTIVE
        session._is_initiator = data.get("is_initiator")
        # 加解密密钥
        session._send_key = base64.b64decode(data["send_key"])
        session._recv_key = base64.b64decode(data["recv_key"])
        session._secret_key_id = data["secret_key_id"]
        session._cipher_suite = data.get("cipher_suite")
        session._key_expires = key_expires
        session._created_at = data.get("created_at", time.time())
        session._active_at = active_at
        # 握手阶段的属性（ACTIVE 状态不再使用，但需设置以防止 AttributeError）
        session._did_private_key = None
        session._did_public_key_hex = ""
        session._eph_private_key = None
        session._eph_public_key = None
        session._eph_public_key_hex = ""
        session._local_key_share = {}
        session._local_random = ""
        session._peer_random = None
        return session

    # ------------------------------------------------------------------
    # 内部处理方法
    # ------------------------------------------------------------------

    def _handle_source_hello(
        self, content: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """处理 SourceHello：创建 responder 会话，返回 [dest_hello, finished]。"""
        peer_did = content.get("source_did", "")
        session = E2eeSession(
            local_did=self.local_did,
            did_private_key_pem=self._signing_pem,
            peer_did=peer_did,
        )
        (hello_type, hello_content), (finished_type, finished_content) = (
            session.process_source_hello(content)
        )
        # responder 在 process_source_hello 后进入 HANDSHAKE_COMPLETING 状态
        self._key_manager.register_pending_session(session)
        return [(hello_type, hello_content), (finished_type, finished_content)]

    def _handle_destination_hello(
        self, content: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """处理 DestinationHello：查找 pending session，返回 [finished]。"""
        session_id = content.get("session_id", "")
        session = self._key_manager.get_pending_session(session_id)
        if session is None:
            logger.warning("找不到 session_id=%s 的握手会话", session_id)
            return []

        finished_type, finished_content = session.process_destination_hello(content)
        return [(finished_type, finished_content)]

    def _handle_finished(
        self, content: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """处理 Finished：激活会话。"""
        session_id = content.get("session_id", "")
        session = self._key_manager.get_pending_session(session_id)
        if session is None:
            logger.warning("找不到 session_id=%s 的握手会话", session_id)
            return []

        session.process_finished(content)
        if session.state == SessionState.ACTIVE:
            self._key_manager.promote_pending_session(session_id)
            logger.info(
                "E2EE 会话激活: %s <-> %s (session_id=%s)",
                session.local_did,
                session.peer_did,
                session_id,
            )
        return []

    def _handle_error(
        self, content: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """处理 E2EE Error：记录日志，返回空列表。"""
        error_code = content.get("error_code", "unknown")
        secret_key_id = content.get("secret_key_id", "")
        logger.warning(
            "收到 E2EE 错误: code=%s, secret_key_id=%s", error_code, secret_key_id
        )
        # 如果是密钥过期或找不到，移除对应会话
        session = self._key_manager.get_session_by_key_id(secret_key_id)
        if session is not None:
            self._key_manager.remove_session(session)
        return []


__all__ = ["E2eeClient"]
