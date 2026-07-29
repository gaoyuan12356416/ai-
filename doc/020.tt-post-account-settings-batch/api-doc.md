# API 文档

## 接口列表

- `POST /api/admin/tt-posts/account-settings/batch/creator-info`
- `POST /api/admin/tt-posts/account-settings/batch`

两者均要求登录、`tt_posts` 权限、同源 JSON；响应 `Cache-Control: no-store`，禁止包含 Token 或 Authorization。

## 请求/响应

### 批量能力检测

请求：

```json
{"source_account_ids": ["640", "641"]}
```

响应：

```json
{
  "items": [
    {
      "source_account_id": "640",
      "account_name": "Dramawave popular reels",
      "account_settings": {"configured": true, "version": 1},
      "creator_info": {
        "creator_nickname": "Dramawave popular reels",
        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
        "comment_disabled": false,
        "duet_disabled": false,
        "stitch_disabled": false
      }
    }
  ],
  "common_capabilities": {
    "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
    "comment_disabled": false,
    "duet_disabled": false,
    "stitch_disabled": false
  }
}
```

`common_capabilities` 是所有目标账号能力的交集。任一账号检测失败时整个请求失败。

### 批量保存

请求：

```json
{
  "targets": [
    {"source_account_id": "640", "expected_version": 1},
    {"source_account_id": "641", "expected_version": 0}
  ],
  "privacy_level": "PUBLIC_TO_EVERYONE",
  "allow_comment": true,
  "allow_duet": true,
  "allow_stitch": true,
  "commercial_disclosure": false,
  "brand_organic_toggle": false,
  "brand_content_toggle": false,
  "is_aigc": false
}
```

响应：

```json
{
  "items": [
    {
      "source_account_id": "640",
      "account_settings": {"configured": true, "version": 2}
    },
    {
      "source_account_id": "641",
      "account_settings": {"configured": true, "version": 1}
    }
  ],
  "saved_count": 2
}
```

保存前会再次逐个调用 `creator_info`。全部账号通过后，在同一 SQLite 事务内检查版本并写入。

## 错误码

- `invalid_batch_targets`：目标为空、重复、超过 50 或结构无效。
- `invalid_account_settings_version`：任一目标版本无效。
- `tt_account_settings_version_conflict`：任一账号版本已变化，整批 0 写入。
- `tt_privacy_not_allowed`：至少一个账号不支持所选隐私范围。
- `tt_interaction_not_allowed`：至少一个账号不允许请求开启的互动能力。
- `tt_batch_creator_info_failed`：至少一个账号实时能力检测失败。

## 兼容性说明

- 既有单账号 `POST /api/admin/tt-posts/account-settings` 保持不变。
- 不修改 `tt_post_account_setting` schema。
- 不修改发布池、队列、GPU 制作和 TikTok 发布接口。
