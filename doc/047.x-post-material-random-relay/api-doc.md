# 内部 API 合同

## 结论

无新增公开 API、无新增路由、无 UI 变更。

## `POST /internal/posts/premium-relay/accounts`

请求继续使用 `run_date` 与 canonical `drama_language`。响应 relay 必须是 active、approved、public、Token 当前确认 `basic|premium|premium_plus` 且 long-video publish eligible。

## `POST /internal/posts/schedule-plan`

material candidate 新合法组合：

```json
{
  "source_type": "material",
  "account_id": 2,
  "preflight_duration": 180.0,
  "delivery_mode": "premium_relay_repost",
  "relay_account_id": 10,
  "relay_account_username": "premium10"
}
```

约束：仅正式 schedule；`account_id` 为目标个号；duration 必须 `>140`；relay 与 target 不同且同语言。每个 material relay option 必须显式携带合法 canonical `drama_language`，缺失或非法均拒绝。当前资格复核失败返回 `x_post_premium_relay_unavailable`，整批不建 queue。

## 兼容性

- direct material payload 不变。
- drama relay payload/least-load 不变。
- manual、X Auto 不接受此 material relay 组合。
