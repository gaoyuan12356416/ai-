# API 文档

## 内部只读会员源列表

`POST /internal/posts/premium-relay/accounts`

请求：`{"run_date":"YYYY-MM-DD"}`。

响应每项仅含安全账号 DTO 与 `relay_assignment_count`；按 `(relay_assignment_count, account_id)` 排序。计数为历史累计已冻结中转任务数，不在午夜清零。调用时实时刷新 token 账号快照，仅返回 active、已批准、会员、公开账号；临时校验错误不会把账号永久写成异常状态。

## 现有计划接口扩展

`POST /internal/posts/schedule-plan` 的 candidate 可冻结：

```json
{
  "delivery_mode": "premium_relay_repost",
  "relay_account_id": 10,
  "relay_account_username": "premium10",
  "preflight_duration": 180.0
}
```

服务端不信任候选中转账号，使用当前安全会员列表并在同一 SQLite 事务内重新均衡。

## X 官方接口

`POST https://api.x.com/2/users/{target_x_user_id}/retweets`

请求：`{"tweet_id":"<source_post_id>"}`。必须收到 HTTP 200 且 `data.retweeted=true` 才确认成功。缺失确认、5xx、网络断开均按未知结果处理。

## 错误码

- `x_post_premium_relay_unavailable`：无合格会员源，未执行 X 写入。
- `x_post_relay_reassignment_fenced`：原帖已开始，禁止换源。
- `x_repost_outcome_unknown` / `x_publish_unknown`：Repost 结果无法确认，禁止自动重试。
