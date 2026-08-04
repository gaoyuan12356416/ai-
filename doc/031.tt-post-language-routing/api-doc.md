# API 文档

## 通用语言契约

- `drama_language` 和用于匹配的 `material_language` 先 trim、casefold，并把 `_` 改为 `-`。
- 空值或缺失值按 `en`。
- 规范值长度 1 至 32；连字符分段必须非空且只含 Unicode 字母或数字。
- 只做规范值 exact match；`en` 不匹配 `English`，`pt` 不匹配 `pt-br`。

## 接口列表

| 方法 | 路径 | 变化 |
| --- | --- | --- |
| GET | `/api/admin/tt-posts/accounts` | `account_settings.drama_language` |
| GET | `/api/admin/tt-posts/account-settings` | 同账号列表 |
| POST | `/api/admin/tt-posts/account-settings` | 单账号保存 `drama_language` |
| POST | `/api/admin/tt-posts/account-settings/batch` | 批量保存共同 `drama_language` |
| POST | `/api/admin/tt-posts/materials/preview` | 继续返回权威 `material_language` |
| GET | `/api/admin/tt-posts/material-pool` | 继续返回 `material_language` 和实际/待领取账号语义 |
| GET/POST | `/api/admin/tt-posts/schedule` | 返回自动语言池数量与手动精确账号数量 |
| POST | `/internal/tt-posts/schedules/due` | 请求无业务字段；服务端按语言领取 |
| POST | `/api/admin/tt-posts/run-now` | 结构不变；手动精确账号，不跨池换号 |

## 请求/响应

### 单账号设置

请求示例：

```json
{
  "source_account_id": "101",
  "drama_language": "pt_BR",
  "privacy_level": "PUBLIC_TO_EVERYONE",
  "allow_comment": false,
  "allow_duet": false,
  "allow_stitch": false,
  "commercial_disclosure": false,
  "brand_organic_toggle": false,
  "brand_content_toggle": false,
  "is_aigc": false,
  "expected_version": 3
}
```

成功响应中的保存值为规范形式：

```json
{
  "item": {
    "source_account_id": "101",
    "account_settings": {
      "configured": true,
      "drama_language": "pt-br",
      "version": 4
    }
  }
}
```

未发送或发送空 `drama_language` 时保存为 `en`。其他账号设置字段和 Creator Info 校验合同不变。

### 批量账号设置

`targets` 仍逐账号携带 `source_account_id` 和 `expected_version`；顶层增加共同的 `drama_language`。批量保存保持原子：任一账号版本冲突或字段无效时不修改任何账号。

### 素材校验与素材池

素材语言只能来自服务端只读解析：

```json
{
  "item": {
    "material_id": "5921463",
    "content_id": "zALq8tHA9a",
    "material_language": "en",
    "status": "validated"
  }
}
```

客户端不得覆盖 `material_language`。缺少同语言账号不改变校验成功或预制作状态；完成后记录保持 `available`。

### 自动领取

调用方不能提交账号语言、素材语言、素材 ID 或目标账号覆盖。服务端读取到期 schedule 的账号和当前本地设置，在数据库事务中领取同语言 FIFO。

- 命中：run/pool/queue 使用实际到期账号。
- 未命中：返回现有安全跳过/池空语义，不创建 queue。
- 恢复已有 run：使用原 run/pool，不重选。

schedule 响应中，`available_material_count` 表示该账号语言在全局池中可供自动领取的数量；`manual_available_material_count` 表示该账号精确分片可供 run-now 的数量。`can_publish_now` 只使用后者，避免其他账号的同语言素材错误启用手动按钮。

## 错误码

| 错误码 | HTTP | 说明 |
| --- | --- | --- |
| `invalid_drama_language` | 400 | 语言长度、分段或字符不合法 |
| `invalid_account_settings` | 400 | 账号设置字段不完整或含未知字段 |
| `tt_account_settings_version_conflict` | 409 | 单条或批量乐观版本冲突 |
| `tt_account_settings_required` | 409 | 自动账号尚无本地发布设置 |
| `tt_post_recurring_pool_language_empty` | 409 | 自动发布账号当前剧语言没有可用素材；不 fallback，素材保持 `available` |
| `tt_post_recurring_pool_empty` | 409 | 手动发布的精确账号分池没有可用素材 |
| `tt_post_schedule_not_current` | 409 | 自动排期关闭或版本已变化 |
| `tt_post_account_publish_busy` | 409 | 账号已有活跃发布运行/队列 |

具体错误文本应脱敏，不返回 Token、内部凭证或 SQL。

## 兼容性说明

- 旧 SQLite 账号设置行经迁移得到 `drama_language='en'`。
- 旧请求缺少或传空 `drama_language` 按 `en`，不会产生 NULL/空串。
- 历史空 `material_language` 在领取和页面展示时按 `en`。
- 非空历史语言不做别名映射；不符合新格式的历史值保持可见但被隔离，不参与自动路由。
- 内部 `routing_language` 只用于持久化规范键和复合索引，不通过 API 暴露。
- queue/run API 不增加客户端可控语言字段，避免伪造路由。
- 立即发布测试接口和 Direct Post/GPU 协议不变。
