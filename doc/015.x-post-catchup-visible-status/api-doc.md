# API 文档

所有接口仅允许 loopback daily bearer，响应不得包含 Token、内部 bearer 或数据库凭据。

## 查询补发子批次

`POST /internal/posts/catchup-plan/query`

```json
{
  "run_date": "2026-07-27",
  "parent_run_id": 4,
  "reason": "scope_expansion_v1"
}
```

返回 `{found, run, queues}` 的安全身份信息；存在时只能恢复冻结队列。

## 创建补发子批次

`POST /internal/posts/catchup-plan`

```json
{
  "run_date": "2026-07-27",
  "source_date": "2026-07-26",
  "parent_run_id": 4,
  "reason": "scope_expansion_v1",
  "candidates": []
}
```

服务端以当前 `X_POST_DAILY_ACCOUNT_IDS` 减去当天既有账号队列计算精确目标，不接受调用方覆盖账号范围。整批账号/素材/媒体校验通过后原子建批。

## 记录预检失败

`POST /internal/posts/catchup-runs/record-failure`

请求包含日期、父批次、固定原因、`expected_missing_count` 和脱敏错误；仅在子批次未创建正式队列时记录。

## 发布正式补发队列

继续使用：

`POST /internal/posts/queue/{queue_id}/publish`

队列必须属于当前配置账号，并且恰好有一个正式父键：`run_id` 或 `catchup_run_id`。

## 接口列表

## 请求/响应

## 错误码

## 兼容性说明

## 一次性运行入口

`x-post-catchup.service` 是手工 oneshot，不存在配套 Timer。它从
`/etc/x-post-catchup.env` 读取已批准的日期、缺失账号数和原因，并复用
daily 的 root-only 环境、媒体修复配置、同一 flock 和 systemd 沙箱。
本次 runner 在代码层再次硬钉 `2026-07-27 / 6 / scope_expansion_v1`；
任一参数不一致均在访问账号、素材或 X 之前失败。
