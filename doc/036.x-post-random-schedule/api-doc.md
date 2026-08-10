# API 变更

## 保存随机设置

`PUT /api/x-posts/schedule-settings/material`（短剧池使用 `drama`）

```json
{
  "enabled": true,
  "timezone": "Asia/Shanghai",
  "account_ids": [16, 14],
  "publish_times": [],
  "schedule_mode": "random",
  "random_daily_count": 6,
  "body_template": "Watch now 👉{{url}}\n🎬 {{drama_name}}\n{{desc}}",
  "version": 15
}
```

随机模式的 `publish_times` 必须为空；次数为 1–24。固定模式仍使用 `publish_times`，服务端将随机次数归零。

## 响应新增字段

```json
{
  "schedule_mode": "random",
  "random_daily_count": 6,
  "random_effective_date": "2026-08-11",
  "posts_per_day": 12,
  "next_due_at": "2026-08-11T01:17+08:00",
  "random_daily_plans": [
    {
      "run_date": "2026-08-11",
      "config_version": 16,
      "account_ids": [16, 14],
      "publish_times": ["01:17", "04:28"]
    }
  ]
}
```

所有时间点均为北京时间。错误码新增 `invalid_schedule_mode`、`invalid_random_daily_count`、`x_post_random_times_must_be_empty` 和 `x_post_random_plan_generation_failed`。
