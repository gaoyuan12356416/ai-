# API 文档

## 接口列表

- `POST /api/admin/x-accounts/{account_id}/publish-approval`
- `POST /internal/accounts/{account_id}/publish-approval`（仅主服务内部使用）

## 请求/响应

请求：

```json
{"approved": true}
```

成功响应：

```json
{
  "item": {
    "id": 16,
    "publish_approved": true,
    "credential_publish_eligible": true,
    "publish_eligible": true
  }
}
```

管理员接口要求后台 cookie 登录、管理员角色和同源 JSON 请求。

## 错误码

- `invalid_request`：账号 ID 或 `approved` 类型无效。
- `x_admin_required`：非管理员修改。
- `x_account_not_found`：记录不存在。
- `x_account_publish_not_approved`：最终发布时账号未获许可，HTTP 409。

## 兼容性说明

账号列表原有 `publish_eligible` 字段保留，但现在表示“账号凭据有效且管理员允许发布”。新增 `credential_publish_eligible` 表示纯凭据状态。旧客户端会自动得到更严格的安全过滤。
