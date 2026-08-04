# API 文档

## `POST /api/admin/tt-posts/material-pool`

新页面请求示例：

```json
{
  "idempotency_key": "tt-post:<uuid>:5921117",
  "material_id": "5921117",
  "content_id": "ZALQ8tHA9a",
  "expected_config_version": 5,
  "caption_template": "...",
  "consent": {
    "accepted": true,
    "version": "tt-recurring-post-consent-20260730",
    "accepted_at": "2026-08-04T00:00:00Z"
  }
}
```

`source_account_id` 在携带 `expected_config_version` 时可选。服务端从该版本已保存的自动发布账号中稳定分配，并在响应 `item.source_account_id` 返回实际账号。

## 兼容性

- 旧调用可继续显式传 `source_account_id`。
- 未传 `expected_config_version` 的 legacy 请求仍必须显式传账号。
- 响应结构、幂等键、错误 envelope 不变。

## 主要错误码

- `tt_post_auto_config_version_conflict`：配置不存在、版本过期或不匹配。
- `tt_post_auto_accounts_required`：已保存配置没有账号。
- `tt_post_auto_account_not_selected`：显式账号不属于已保存配置。
- `tt_post_account_settings_missing`：分配账号缺少本地发布设置。
- `tt_content_id_mismatch`：页面 Drama ID 与素材真实映射不一致。
