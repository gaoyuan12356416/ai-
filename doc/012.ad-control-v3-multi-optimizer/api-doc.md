# API 文档

## 接口列表

- `GET /api/ad-control/v3/meta`
- `GET /api/ad-control/v3/rule-groups`
- `POST /api/ad-control/v3/rule-groups`
- `PUT /api/ad-control/v3/rule-groups/{group_id}`
- `GET /api/ad-control/v3/executions`

## 请求/响应

`GET /meta` 普通用户新增示例：

```json
{
  "actor": {"optimizer_id": 387, "optimizer_ids": [387, 686]},
  "optimizers": [
    {"optimizer_id": 387, "name": "王鹏", "locked": true},
    {"optimizer_id": 686, "name": "Lucas", "locked": true}
  ],
  "permissions": {"current_optimizer_id": 387, "current_optimizer_ids": [387, 686]}
}
```

规则组响应继续返回兼容字段 `optimizer_id: 387`，并新增 `optimizer_ids: [387,686]`。客户端创建/更新仍只可提交原有 `optimizer_id`；`optimizer_ids` 是服务端管理字段，提交会被拒绝。

`GET /meta` 管理员本人别名示例：

```json
{
  "actor": {"is_admin": true, "optimizer_id": null, "optimizer_ids": [387, 686]},
  "permissions": {
    "can_select_optimizer": true,
    "current_optimizer_id": null,
    "current_optimizer_ids": [387, 686]
  }
}
```

管理员仍从全部有效优化师中显式选择一个 `optimizer_id`。如果选择值属于 `actor.optimizer_ids`，服务端自动保存并扫描完整本人集合；否则只保存选择的单个优化师。管理员本人身份无法安全解析时 `optimizer_ids` 返回空数组，但管理员下拉与写接口不受影响。

## 错误码

- `optimizer_identity_unresolved` 403：无有效优化师映射。
- `optimizer_identity_ambiguous` 409：不可信层多命中。
- `optimizer_identity_too_large` 409：别名超过 20。
- `optimizer_forbidden` 403：请求范围不属于当前用户。

## 兼容性说明

旧客户端可继续只读取 `optimizer_id`/`optimizer_name`；新 UI 优先使用数组字段。管理员选择其他人时仍为单优化师，选择本人别名时由服务端扩展。所有审计时间字段继续以 UTC 存储、UTC+8 展示。
