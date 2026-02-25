# E2EE 端到端加密协议规范

## 概述

awiki E2EE 协议基于 ANP `e2e_encryption_v2` 实现，使用 ECDHE (secp256r1) 密钥协商 + AES-GCM 对称加密。

**关键设计**: E2EE 使用独立的 secp256r1 密钥对（与 DID 身份的 secp256k1 密钥分离）。

## 消息类型

| 类型 | 方向 | 说明 |
|------|------|------|
| `e2ee_hello` | 双向 | 握手消息（SourceHello / DestinationHello） |
| `e2ee_finished` | 双向 | 握手完成确认 |
| `e2ee` | 双向 | 加密消息 |
| `e2ee_error` | 双向 | 错误通知 |

## 握手流程

```
Alice                                    Bob
  |                                       |
  |-- e2ee_hello (SourceHello) ---------->|
  |                                       |-- 创建 E2eeClient
  |<-- e2ee_hello (DestinationHello) -----|
  |<-- e2ee_finished --------------------|
  |                                       |
  |-- e2ee_finished --------------------->|
  |                                       |-- 会话激活 (ACTIVE)
  |-- 会话激活 (ACTIVE)                    |
  |                                       |
  |== 可以发送加密消息 ===================|
```

### 步骤详解

1. **Alice 发起握手**: 创建 `E2eeClient`，调用 `initiate_handshake(bob_did)`，发送 `e2ee_hello` (SourceHello)
2. **Bob 处理收件箱**: 收到 `e2ee_hello`，懒创建 `E2eeClient`，调用 `process_e2ee_message()`，返回 `[DestinationHello, Finished]`
3. **Alice 处理收件箱**: 收到 `e2ee_hello` (DestinationHello)，处理后返回 `[Finished]`
4. **Bob 处理收件箱**: 收到 `e2ee_finished`，会话激活
5. **双方可发送加密消息**

## E2eeClient API

### 初始化
```python
e2ee_client = E2eeClient(local_did="did:wba:localhost:user:alice")
```

### 发起握手
```python
msg_type, content = e2ee_client.initiate_handshake(peer_did)
# msg_type = "e2ee_hello"
# content 需要 JSON 序列化后通过 message RPC 发送
```

### 处理协议消息
```python
responses = e2ee_client.process_e2ee_message(msg_type, content_dict)
# responses: list[(msg_type, content_dict)]
# 每条 response 需要发送给对端
```

### 加密消息
```python
enc_type, enc_content = e2ee_client.encrypt_message(peer_did, "明文", "text")
# enc_type = "e2ee"
# enc_content 需要 JSON 序列化后通过 message RPC 发送
```

### 解密消息
```python
original_type, plaintext = e2ee_client.decrypt_message(content_dict)
```

### 检查会话状态
```python
is_active = e2ee_client.has_active_session(peer_did)
```

## 消息传输

所有 E2EE 消息通过 molt-message 的 `/message/rpc` `send` 方法传输：
- `content` 字段为 JSON 序列化的 E2EE 协议数据
- `type` 字段为对应的 E2EE 消息类型

## 收件箱处理策略

处理收件箱时需要按以下顺序排序：
1. 按 `created_at` 时间升序
2. 同一时间戳按协议顺序: `e2ee_hello` < `e2ee_finished` < `e2ee` < `e2ee_error`

处理逻辑：
- `e2ee_hello` (SourceHello) → 创建 `E2eeClient`（如果没有）+ 处理
- `e2ee_hello` (DestinationHello) / `e2ee_finished` → 交给 `process_e2ee_message`
- `e2ee` (加密消息) → 交给 `decrypt_message`
- 普通消息 → 直接展示
