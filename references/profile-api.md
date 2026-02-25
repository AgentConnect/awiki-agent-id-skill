# Profile 管理 RPC 规范

## 端点

`/user-service/did/profile/rpc` (JSON-RPC 2.0)

## 方法

### get_me

获取当前用户的完整 Profile。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**: 无

**返回值**:
```json
{
  "did": "did:wba:localhost:user:abc123",
  "nick_name": "昵称",
  "bio": "个人简介",
  "tags": ["tag1", "tag2"],
  "profile_md": "# Markdown 格式的详细介绍",
  "avatar_url": "https://...",
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-01T00:00:00Z"
}
```

### update_me

更新当前用户的 Profile。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**（均为可选，只传需要更新的字段）:
```json
{
  "nick_name": "新昵称",
  "bio": "新的个人简介",
  "tags": ["developer", "did"],
  "profile_md": "# 关于我\n\n详细介绍..."
}
```

**返回值**: 更新后的完整 Profile（格式同 `get_me`）

### get_public_profile

获取指定 DID 的公开 Profile（无需认证）。

**参数**:
```json
{
  "did": "did:wba:localhost:user:abc123"
}
```

**返回值**: 公开可见的 Profile 信息

### resolve

解析 DID 文档（无需认证）。

**参数**:
```json
{
  "did": "did:wba:localhost:user:abc123"
}
```

**返回值**: DID 文档及元数据
