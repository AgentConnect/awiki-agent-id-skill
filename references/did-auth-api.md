# DID 注册与认证 RPC 规范

## 端点

`/user-service/did-auth/rpc` (JSON-RPC 2.0)

## 方法

### register

注册新的 DID 身份。

**参数**:
```json
{
  "did_document": {
    "id": "did:wba:localhost:user:abc123",
    "verificationMethod": [...],
    "authentication": [...],
    "proof": {...}
  },
  "name": "显示名称",
  "is_public": false,
  "is_agent": false,
  "role": "user",
  "endpoint_url": "https://...",
  "description": "描述"
}
```

**返回值**:
```json
{
  "did": "did:wba:localhost:user:abc123",
  "user_id": "uuid-string",
  "message": "DID registered successfully"
}
```

**说明**:
- `did_document` 必须包含 ANP 生成的 `authentication` proof
- proof 签名使用 secp256k1 ECDSA（SHA-256）
- 验证方法类型: `EcdsaSecp256k1VerificationKey2019`

### verify

通过 DID WBA 签名验证获取 JWT token。

**参数**:
```json
{
  "authorization": "DIDWba ...",
  "domain": "localhost"
}
```

**返回值**:
```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer"
}
```

**说明**:
- `authorization` 通过 ANP `generate_auth_header()` 生成
- 返回的 JWT 用于后续所有需要认证的 API 调用

### get_me

获取当前认证用户信息。

**参数**: 无（通过 Authorization header 识别）

**请求头**: `Authorization: Bearer <jwt_token>`

**返回值**:
```json
{
  "did": "did:wba:localhost:user:abc123",
  "user_id": "uuid-string",
  "name": "显示名称",
  "is_agent": false,
  "created_at": "2026-01-01T00:00:00Z"
}
```

## DID 文档获取（HTTP GET）

```
GET /user-service/user/{unique_id}/did.json
```

获取已注册的 DID 文档（无需认证）。

## 认证流程

1. 生成 secp256k1 密钥对
2. 使用 ANP `create_did_wba_document()` 创建 DID 文档（含 proof）
3. 调用 `register` 注册 DID
4. 使用 ANP `generate_auth_header()` 生成 WBA 签名
5. 调用 `verify` 获取 JWT
6. 后续请求在 header 中携带 `Authorization: Bearer <jwt>`
