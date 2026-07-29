# API 文档

所有接口均由 AI 后台同源代理，要求登录、`tt_posts` 权限；POST 另要求同源 JSON。响应禁止包含 Token 或 Authorization。

## GET `/api/admin/tt-posts/account-settings`

返回安全账号列表及配置状态。

```json
{
  "items": [
    {
      "source_account_id": "101",
      "account_name": "DramaWave",
      "publish_eligible": true,
      "account_settings": {
        "configured": true,
        "privacy_level": "SELF_ONLY",
        "allow_comment": true,
        "allow_duet": false,
        "allow_stitch": false,
        "brand_organic_toggle": true,
        "brand_content_toggle": false,
        "commercial_disclosure": true,
        "is_aigc": false,
        "version": 2,
        "updated_at": "2026-07-29T04:00:00Z"
      }
    }
  ],
  "gates": {
    "live_enabled": false,
    "direct_audit_approved": false,
    "url_property_verified": false
  }
}
```

未配置账号的 `account_settings` 为：

```json
{"configured": false}
```

## POST `/api/admin/tt-posts/account-settings/creator-info`

请求：

```json
{"source_account_id": "101"}
```

返回脱敏后的昵称、用户名、可用隐私范围、互动禁用标记和最长视频时长。

## POST `/api/admin/tt-posts/account-settings`

请求：

```json
{
  "source_account_id": "101",
  "privacy_level": "SELF_ONLY",
  "allow_comment": true,
  "allow_duet": false,
  "allow_stitch": false,
  "commercial_disclosure": true,
  "brand_organic_toggle": true,
  "brand_content_toggle": false,
  "is_aigc": false,
  "expected_version": 2
}
```

首次保存使用 `expected_version=0`。更新必须传当前版本；保存前服务端实时调用 `creator_info`。

主要错误：

- `invalid_account_settings_version`：版本缺失或无效。
- `tt_account_settings_version_conflict`：配置已被其他操作更新。
- `tt_commercial_disclosure_invalid`：商业披露选项不一致。
- `tt_privacy_not_allowed`：隐私范围不在实时能力中。
- `tt_interaction_not_allowed`：请求开启 TikTok 当前禁用的互动能力。

## POST `/api/admin/tt-posts/queue` 变更

客户端不再提交隐私、互动、商业披露和 AI 声明。请求仍包含账号、素材、Drama ID、时间、固定描述、发布模式和当次同意。服务端要求账号已配置，并将当前配置冻结到队列。
