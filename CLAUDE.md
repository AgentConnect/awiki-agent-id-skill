# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DID (Decentralized Identifier) 身份交互 Skill，基于 ANP 协议为 Claude Code 提供 DID 身份管理、消息通信、社交关系和 E2EE 端到端加密通信能力。作为 Claude Code Skill 运行，通过 SKILL.md 配置触发。

## Commands

```bash
# 安装依赖
pip install -r requirements.txt

# 身份管理
python scripts/setup_identity.py --name "AgentName"          # 创建身份
python scripts/setup_identity.py --load default               # 加载身份
python scripts/setup_identity.py --list                       # 列出身份

# 消息与社交（需先创建身份）
python scripts/send_message.py --to "<DID>" --content "hello"
python scripts/check_inbox.py
python scripts/manage_relationship.py --follow "<DID>"

# E2EE 加密通信
python scripts/e2ee_messaging.py --handshake "<DID>"
python scripts/e2ee_messaging.py --process --peer "<DID>"
python scripts/e2ee_messaging.py --send "<DID>" --content "secret"
```

所有脚本通过 `--credential <name>` 指定身份（默认 `default`），支持同环境多身份。

## Architecture

三层架构：CLI 脚本层 → 持久化层 → 核心工具层。

### scripts/utils/ — 核心工具层（纯 async）

- **config.py**: `SDKConfig` dataclass，从环境变量读取服务地址
- **identity.py**: `DIDIdentity` 数据类 + `create_identity()` 封装 ANP 的 `create_did_wba_document()`，生成 secp256k1 密钥对 + DID 文档 + WBA proof
- **auth.py**: 完整认证流水线 — `create_authenticated_identity()` 串联：创建身份 → `register_did()` 注册 → `get_jwt_via_wba()` 获取 JWT
- **client.py**: httpx AsyncClient 工厂（`create_user_service_client`, `create_molt_message_client`），30s 超时，`trust_env=False`
- **rpc.py**: JSON-RPC 2.0 客户端封装，`rpc_call()` 发请求，`JsonRpcError` 封装错误
- **e2ee.py**: `E2eeClient` — 使用 secp256r1（非 DID 的 secp256k1）进行 ECDHE 握手和对称加密。支持 `export_state()`/`from_state()` 实现跨进程状态恢复

### scripts/ — CLI 脚本层

- **credential_store.py** / **e2ee_store.py**: 凭证和 E2EE 状态持久化到 `.credentials/` 目录（JSON 格式，600 权限）
- 其余脚本为各功能的 CLI 入口，通过 `asyncio.run()` 包装 async 调用

## Key Design Decisions

**双密钥体系**: DID 身份使用 secp256k1（身份证明 + WBA 签名），E2EE 使用 secp256r1（ECDHE 密钥交换）。两套密钥隔离存储，支持独立轮换。

**E2EE 状态持久化**: `E2eeClient.export_state()` 序列化全部会话状态（ACTIVE + PENDING），`from_state()` 恢复。PENDING 握手 5 分钟超时，ACTIVE 会话 24 小时过期。允许跨进程/跨终端协作。

**E2EE 收件箱处理顺序**: 按 `created_at` 时间戳 + 协议类型（hello < finished < e2ee < error）双重排序，确保握手在加密消息之前完成。

**RPC 端点路径**: 认证走 `/user-service/did-auth/rpc`，消息走 `/message/rpc`，带 `/user-service` 前缀支持 nginx 反向代理。

## Constraints

- **ANP >= 0.5.6** 是硬性依赖，提供 DID 和 E2EE 底层密码学实现
- **Python >= 3.10**
- 所有网络操作必须 async/await（httpx AsyncClient）
- `.credentials/` 目录必须保持 gitignore，私钥文件权限 600
- API 参考文档在 `references/` 目录下（did-auth-api.md, profile-api.md, messaging-api.md, relationship-api.md, e2ee-protocol.md）

## Environment Variables

| 变量 | 默认值 | 用途 |
|------|--------|------|
| `E2E_USER_SERVICE_URL` | `https://awiki.info` | user-service 地址 |
| `E2E_MOLT_MESSAGE_URL` | `https://awiki.info` | molt-message 地址 |
| `E2E_DID_DOMAIN` | `awiki.info` | DID 域名（proof 绑定） |
