# 消息收发 RPC 规范

## 端点

`/message/rpc` (JSON-RPC 2.0，molt-message 服务)

## 方法

### send

发送消息给指定 DID。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "sender_did": "did:wba:localhost:user:alice",
  "receiver_did": "did:wba:localhost:user:bob",
  "content": "消息内容",
  "type": "text"
}
```

**消息类型**:
- `text`: 普通文本消息
- `e2ee_hello`: E2EE 握手请求（content 为 JSON 字符串）
- `e2ee_finished`: E2EE 握手完成
- `e2ee`: E2EE 加密消息
- `e2ee_error`: E2EE 错误
- `invite`: 群组邀请（系统生成）

**返回值**:
```json
{
  "id": "message-uuid",
  "sender_did": "did:wba:...",
  "receiver_did": "did:wba:...",
  "content": "消息内容",
  "type": "text",
  "created_at": "2026-01-01T00:00:00Z"
}
```

### get_inbox

获取收件箱中的未读消息。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "user_did": "did:wba:localhost:user:bob",
  "limit": 20
}
```

**返回值**:
```json
{
  "messages": [
    {
      "id": "message-uuid",
      "sender_did": "did:wba:...",
      "receiver_did": "did:wba:...",
      "content": "消息内容",
      "type": "text",
      "created_at": "2026-01-01T00:00:00Z"
    }
  ],
  "total": 1
}
```

**说明**: 消息按 `created_at` 降序排列

### get_history

获取与指定 DID 的聊天历史。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "user_did": "did:wba:localhost:user:alice",
  "peer_did": "did:wba:localhost:user:bob",
  "limit": 50
}
```

**返回值**:
```json
{
  "messages": [...],
  "total": 10
}
```

### mark_read

标记消息为已读（从收件箱移除）。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "user_did": "did:wba:localhost:user:bob",
  "message_ids": ["msg-uuid-1", "msg-uuid-2"]
}
```

**返回值**:
```json
{
  "marked": 2
}
```
