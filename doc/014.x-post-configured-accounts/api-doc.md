# 014.x-post-configured-accounts API 文档

## 接口列表

本需求不新增公网路由，只扩展既有账号列表 DTO，并把既有内部 daily 接口的固定三条合同改为配置数量 N。

| 接口 | 变化 |
| --- | --- |
| `GET /api/admin/x-accounts` | 每个账号增加 `daily_auto_publish_configured` 布尔值 |
| `GET /api/x-accounts` | 共用账号 DTO，同样增加该布尔值 |
| `POST /internal/posts/daily-plan` | daily bearer 提交的候选数必须等于当前配置账号数 N |
| `POST /internal/posts/daily-plan/query` | 读取同日冻结计划；配置扩容不追加 queue |
| `POST /internal/posts/queue/{queue_id}/publish` | 动态账号范围鉴权，发布状态机不变 |

## 账号列表响应

示例使用虚构账号：

```json
{
  "items": [
    {
      "id": 1001,
      "username": "example_account",
      "status": "active",
      "daily_auto_publish_configured": true
    },
    {
      "id": 1002,
      "username": "another_account",
      "status": "active",
      "daily_auto_publish_configured": false
    }
  ],
  "total": 2,
  "updated_at": "2026-07-27T02:00:00Z"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `daily_auto_publish_configured` | boolean | 当前账号行 ID 是否属于 sidecar 已解析的 `X_POST_DAILY_ACCOUNT_IDS` |

- 字段必须始终存在且为 JSON boolean，不能返回 `"true"`、`1` 或空值。
- 该字段不表示账号活跃、Token 健康或当天发布成功。
- 响应不得新增 `daily_account_ids`、完整配置列表、env 内容、Token 或其他凭证。
- 既有 Cookie、管理员权限和 `Cache-Control: no-store` 合同不变。

## 内部动态批次合同

- 配置数量 `N` 允许 1 至 50。
- 新计划的 `candidates`、响应 `queues`、账号集合和顺序必须与配置完全一致。
- N 个账号和 N 个素材完成全批预检后，才允许创建计划。
- 同日已有有 queue 的冻结计划时，query 返回历史身份和数量；配置从 3 扩至 9 不会生成额外 6 条。
- 已发布、失败和未知结果仍按现有幂等/停止策略处理。

## 错误原则

- 非法账号配置或超过上限：服务/任务启动失败，零 X 写入。
- runner 与 sidecar 配置不一致：账号范围校验失败，零 X 写入。
- 候选数量、账号集合或响应顺序不匹配：fail closed。
- 账号不可发布或素材不足：保持现有 `failed_preflight` 语义。
- 未知写结果：保持 `needs_review`，不得自动重试。

不为本需求凭空新增公开错误码；以实际实现中的稳定错误码和测试结果为准。

## 兼容性说明

- 新字段为只读增量字段，忽略未知字段的旧客户端不受影响。
- 无数据库迁移。
- 历史三账号计划继续可查询和审计，不按当前九账号配置改写。
- 管理员 UI 仅使用逐账号布尔字段，不依赖或展示完整配置集合。
