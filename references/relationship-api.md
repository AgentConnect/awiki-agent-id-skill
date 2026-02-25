# 社交关系 RPC 规范

## 端点

`/user-service/did/relationships/rpc` (JSON-RPC 2.0)

## 关系方法

### follow

关注指定 DID。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "target_did": "did:wba:localhost:user:bob"
}
```

**返回值**:
```json
{
  "target_did": "did:wba:localhost:user:bob",
  "is_following": true,
  "is_friend": false
}
```

**说明**: 当双方互相关注时 `is_friend` 为 `true`

### unfollow

取消关注指定 DID。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "target_did": "did:wba:localhost:user:bob"
}
```

**返回值**:
```json
{
  "target_did": "did:wba:localhost:user:bob",
  "is_following": false,
  "is_friend": false
}
```

### get_status

查看与指定 DID 的关系状态。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "target_did": "did:wba:localhost:user:bob"
}
```

**返回值**:
```json
{
  "is_following": true,
  "is_follower": true,
  "is_friend": true,
  "is_blocked": false,
  "is_blocked_by": false
}
```

### get_following

获取关注列表。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "limit": 50,
  "offset": 0
}
```

**返回值**:
```json
{
  "items": [
    {
      "did": "did:wba:...",
      "name": "Bob",
      "is_friend": true
    }
  ],
  "total": 1
}
```

### get_followers

获取粉丝列表。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "limit": 50,
  "offset": 0
}
```

**返回值**: 格式同 `get_following`

## 群组方法

### create_group

创建群组。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "name": "群组名称",
  "description": "群组描述",
  "max_members": 100,
  "is_public": true
}
```

**返回值**:
```json
{
  "id": "group-uuid",
  "name": "群组名称",
  "description": "群组描述",
  "owner_did": "did:wba:...",
  "max_members": 100,
  "is_public": true,
  "created_at": "2026-01-01T00:00:00Z"
}
```

### invite

邀请用户加入群组。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "group_id": "group-uuid",
  "target_did": "did:wba:localhost:user:bob"
}
```

**返回值**:
```json
{
  "invite_id": "invite-uuid",
  "group_id": "group-uuid",
  "target_did": "did:wba:..."
}
```

**说明**: 邀请消息会通过 molt-message 投递到被邀请者的收件箱（type: `invite`）

### join

通过邀请加入群组。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "group_id": "group-uuid",
  "invite_id": "invite-uuid"
}
```

**返回值**:
```json
{
  "group_id": "group-uuid",
  "member_did": "did:wba:...",
  "role": 0
}
```

**角色值**: 0 = 普通成员, 1 = 管理员, 2 = 群主

### get_group_members

查看群组成员列表。

**请求头**: `Authorization: Bearer <jwt_token>`

**参数**:
```json
{
  "group_id": "group-uuid"
}
```

**返回值**:
```json
{
  "members": [
    {
      "did": "did:wba:...",
      "name": "Alice",
      "role": 2,
      "joined_at": "2026-01-01T00:00:00Z"
    }
  ],
  "total": 2
}
```
