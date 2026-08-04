# API 契约

## GET `/api/admin/tt-posts/auto-config`

新增响应字段：

- `schedule_mode`: `fixed` 或 `random`
- `random_daily_count`: `0..24`
- `random_effective_date`: 北京日期；随机配置从该日开始生成
- `random_daily_plans`: 当前/次日已持久化计划数组，每项含账号、日期、配置版本和时间数组

旧配置缺少字段时返回 `fixed / 0 / "" / []`。

## POST `/api/admin/tt-posts/auto-config`

固定模式示例：

```json
{
  "expected_version": 5,
  "enabled": true,
  "timezone": "Asia/Shanghai",
  "schedule_mode": "fixed",
  "publish_times": ["08:15", "13:40", "21:05"],
  "random_daily_count": 0,
  "source_account_ids": ["101", "102"],
  "caption_template": "Drama ID: {{content_id}}",
  "consent": {"accepted": true, "version": "...", "accepted_at": "..."}
}
```

随机模式将 `publish_times` 传空数组，`random_daily_count` 传 `1..24`。为兼容旧客户端，未传模式时按固定模式处理。
