# API 文档

## 公共后台接口

统一前缀：`/api/admin/x-auto-publish`。GET/POST 均要求后台 Cookie 和对应导航权限；POST 还要求同源 JSON。响应均为 `Cache-Control: no-store`，不会返回 OAuth token、内部 bearer 或素材源 URL。

| 方法 | 路径 | 用途 | 成功状态 |
| --- | --- | --- | --- |
| GET | `/accounts` | 安全 X 账号快照；只读，不调用 X 或刷新 Token | 200 |
| POST | `/accounts/{id}/verify` | 有模板导航权限的操作员逐个刷新并校验账号资格 | 200 |
| GET | `/templates` | 模板列表；支持 `status/q/limit/offset` | 200 |
| GET | `/templates/{id}` | 当前模板及版本配置 | 200 |
| POST | `/templates` | 创建默认停用的模板 | 200 |
| POST | `/templates/{id}` | 以 `expected_version` 创建不可变新版本 | 200 |
| POST | `/templates/{id}/copy` | 复制为独立、默认停用模板 | 200 |
| POST | `/templates/{id}/enable` | 启用当前已确认版本 | 200 |
| POST | `/templates/{id}/disable` | 停用模板 | 200 |
| POST | `/templates/{id}/preview` | 只读预览，不占用素材 | 200 |
| POST | `/templates/{id}/run-now` | 二次确认并按幂等键创建异步运行 | 202 |
| GET | `/runs` | 运行列表；支持模板/触发/状态/日期过滤 | 200 |
| GET | `/runs/{id}` | 运行、账号任务、X queue/log/Post 结果 | 200 |

模板正文使用 X 宏：`{{drama_name}}`、`{{desc}}` 必须各出现一次，`{{url}}` 最多一次。`language` 为 2–32 位小写语言码；自动素材时长范围为 1–600 秒。

### 账号列表与显式刷新

- `/accounts` 返回安全字段，包括 `id`、名称、`status`、`publish_approved`、`publish_eligible` 和订阅类型；页面只可依据这些服务端字段决定是否显示刷新按钮。GET 只读现有快照，严禁隐式调用 X。
- `/accounts/{id}/verify` 只允许 Cookie 登录且具备 `xAutoPublishTemplates` 导航权限的操作员调用，并要求 same-origin 空 JSON 请求。服务端强制 `publish_approved=true`；只有动态状态仍为 `refresh_required` 时才会访问 X 刷新，竞态下已变为 `active` 的同一账号幂等回读而不重复调用 X。不支持批量接口，也不接受浏览器传入状态或审批值作为可信依据。
- 刷新经既有 X sidecar 账号锁执行，校验返回身份与原账号一致。只有回读到 `status=active`、`publish_approved=true`、`publish_eligible=true` 才返回可选择状态。
- 临时网络、限流或上游错误返回安全错误，账号继续保持 `refresh_required`，可稍后重试；明确 `x_token_revoked` 时返回需重新授权状态，不得继续刷新。
- 创建、编辑、启用、预览、立即执行、scheduler 建任务及最终发布继续执行现有严格资格校验；新增刷新接口不绕过任何模板或发布 gate。

写请求由主 API 注入 `_actor={user_id,name}`；浏览器不得提交或看到内部 token。立即运行体至少包含：

```json
{
  "expected_version": 3,
  "confirmed": true,
  "idempotency_key": "operator-request-20260811-001"
}
```

## X auto sidecar 内部接口

仅 `127.0.0.1:18833`，Bearer `X_AUTO_POST_INTERNAL_TOKEN`：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 服务和三道 live gate 状态；不含密钥 |
| GET/POST | `/api/admin/x-auto-publish/...` | 主 API 的窄代理目标 |
| POST | `/internal/x-auto-post/tick` | 只创建到期运行；闭门时返回 held |
| POST | `/internal/x-auto-post/execute-next` | 每 worker 最多领取一个安全阶段 |

三道 gate 关闭时，`execute-next` 只可领取既有 reconciliation；不得 selection、prepare plan 或 publish。

## 既有 X sidecar 桥接

仅 `127.0.0.1:8810`，Bearer `X_POST_AUTO_INTERNAL_TOKEN`，并与 backend/daily token 两两不同：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/internal/posts/auto-template/accounts` | 安全账号列表 |
| POST | `/internal/posts/auto-template/accounts/{id}/verify` | 发布前刷新账号资格 |
| POST | `/internal/posts/auto-template/material-keys/query` | 查询 queue 与运营池全局占用 |
| POST | `/internal/posts/auto-template/runs/create` | 按 external task key 创建唯一执行 run |
| POST | `/internal/posts/auto-template/runs/{id}/query` | 精确读取 canonical run/queue/log |
| POST | `/internal/posts/auto-template/runs/{id}/recover` | 账号锁空闲后精确 fence 并终止遗留状态；不重发 |
| POST | `/internal/posts/auto-template/runs/claim` | 来源隔离的兼容 claim；正常 x_auto 不依赖它 |
| POST | `/internal/posts/auto-template/runs/record-failure` | 无 queue 时记录 `failed_preflight` |
| POST | `/internal/posts/auto-template/plan` | 单账号、单素材、≤600 秒建 canonical queue |
| POST | `/internal/posts/auto-template/queue/{id}/publish` | 仅发布 auto_template 父 run 的队列 |
| POST | `/internal/posts/auto-template/storage/preflight` | 单素材持久盘预检 |

`recover` 返回：

```json
{
  "item": {
    "busy": false,
    "recovered": true,
    "run": {"id": 17, "trigger_source": "auto_template", "status": "stopped"}
  }
}
```

账号锁忙时 `busy=true` 且不改数据库。`post_creating` 或 canonical unknown 只可进入 `needs_review`，不得降级为可重试。

## 稳定错误码

- `x_auto_live_gates_closed`：新运行或新发布被三道 gate 拦截。
- `x_account_publish_not_approved`：账号未获管理员发布批准，禁止刷新和选择。
- `x_auto_account_not_publishable`：刷新后或模板严格校验时账号仍未达到 `active + approved + publish_eligible`。
- `x_token_revoked`：X 明确判定授权已失效，必须重新授权；临时 `x_upstream_error` / `x_post_rate_limited` 不得把账号永久降级。
- `x_auto_template_version_conflict`：`expected_version` 过期。
- `x_auto_material_validation_failed` / `x_auto_duration_out_of_range`：严格 X 校验或 600 秒边界失败。
- `x_auto_publish_outcome_unknown`：结果未知，只允许 query/recover/reconcile。
- `x_auto_recovery_unavailable`：精确恢复暂不可用，任务保留为 retry_wait。
- `x_post_auto_template_scope_mismatch`：现有 X sidecar 检测到来源、账号、素材或父 run 不一致。

## 兼容性说明

既有 manual API 始终写入/领取 `trigger_source=manual`；daily、catchup、schedule 的非 manual parent 继续走原路径。`x_post_manual_run` 只做增量列/索引迁移，旧行默认 `manual`。自动模板的 queue 仍使用原 `x_post_queue` 唯一约束、发布日志、短链和账号 token 锁。
