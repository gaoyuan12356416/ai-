# API 文档

## 接口列表

- `GET /api/admin/x-posts/material-pool`
- `POST /api/admin/x-posts/material-pool`
- Sidecar 内部素材候选查询与队列冻结接口（路径不变）

## 请求/响应

列表请求新增可选筛选：

```text
availability=deferred
```

等待行示例：

```json
{
  "status": "unpublished",
  "availability": "deferred",
  "last_error_code": "drama_not_yet_deliverable",
  "last_error_message": "短剧可投放时间为...，当前尚未到达"
}
```

列表 `summary` 新增 `deferred`。添加响应新增 `deferred_count`；未来时间素材不计入
`available_count` 或 `validation_failed_count`。

## 错误码

- `drama_not_yet_deliverable`：临时等待，不是永久校验失败；无队列、可复检。
- 其他发布错误见 `error-catalog.md`。

## 兼容性说明

- `status` 仍只有 `unpublished` / `published`。
- 旧客户端忽略新增 `summary.deferred` 和 `deferred_count` 仍可工作。
- 历史等待行仅凭既有错误码即可派生新状态，无数据回填和 schema 迁移。
