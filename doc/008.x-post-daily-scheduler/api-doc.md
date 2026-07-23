# X Post 每日发布与日志 API

## 鉴权边界

- Sidecar 后台管理接口：仅 loopback + `X_INTERNAL_TOKEN`。
- daily runner 使用不同的 `X_POST_DAILY_INTERNAL_TOKEN`；它只允许调用本页列出的 storage/material/failure/daily-plan、专用三账号 verify 和正式 queue publish 路由。canary、authorize、通用 accounts query/verify/logout、logs/runs query 均返回 403。
- daily bearer 的账号集合由 Sidecar 自身 `X_POST_DAILY_ACCOUNT_IDS` 固定；必须恰好三个不同正整数。
- AI 后台 `/api/admin/x-posts/*`：仅 Feishu Cookie 管理员；API Token 和普通用户拒绝。
- 所有日志响应 `Cache-Control: no-store`，不返回 OAuth/数据库敏感值。

## POST /internal/posts/queue/{queue_id}/publish

请求体为 `{}`。账号、候选、page、URL 参数全部从已冻结 queue 读取，调用方不得覆盖。daily bearer 只能发布固定三账号且带 `run_id` 的正式日更 queue，不能发布 legacy/canary queue。

发布错误响应会显式携带 `outcome_known` 和 `unknown_outcome`；调用方在字段缺失、冲突或响应不可解析时必须按 unknown 停止。

成功响应安全字段：

```json
{
  "item": {
    "status": "published",
    "log_id": 2,
    "short_url": "https://ai.yingliangads.com/s2l/2.html",
    "post_id": "1234567890",
    "preview_url": "https://x.com/example/status/1234567890"
  }
}
```

## POST /internal/posts/daily-plan

请求 `run_date/source_date/candidates`，其中 candidates 必须恰好三条、账号和素材均不同。每条描述最多 4096 个字符，必须显式提供五类合规计数（全为 0）以及媒体预检 `preflight_sha256/preflight_size`。该路由有独立的 256 KiB UTF-8 JSON 硬上限，其他内部路由仍为 16 KiB。Sidecar 以授权账号表中的 username/page 身份覆盖调用方值，再用一个 SQLite 事务创建 run 和三条 queue。相同日期重入只返回原计划，不生成第二批队列。

响应必须回显相同 run ID、`run_date/source_date` 和请求素材；三条 queue 的 `id/account_id` 必须为互异正整数并与请求账号顺序一致。事务明确回滚的错误返回 `outcome_known=true`；响应丢失或畸形按 plan unknown，不覆盖可能已提交的 run。

## POST /internal/posts/accounts/{account_id}/verify

daily bearer 专用账号校验/Token 刷新入口。Sidecar 只接受固定三个账号 ID；通用 `/internal/accounts/{id}/verify` 对 daily bearer 保持 403。

## POST /internal/posts/storage/preflight

请求体 `{}`。验证固定 `/mnt/data-disk/x-post-automation/s2l` 与 `media-work` 均位于真实挂载盘、路径非符号链接、空间足够，且两个目录都能完成原子写/替换/fsync/删除。daily runner 必须在账号校验、选材和创建计划前调用。

成功只返回：

```json
{"item":{"ready":true,"mounted":true,"atomic_write":true}}
```

## POST /internal/posts/material-keys/query

请求 `material_keys` 或 `material_ids`，必须为 1..1000 个规范正整数。仅返回其中已被任意历史 queue 占用的全局 `material_keys`。

## POST /internal/posts/runs/record-failure

发布计划创建前失败时写入 `failed_preflight` 批次。请求字段为 `run_date/source_date/error_code/error_message`；错误文本先脱敏。同日已存在正式三队列时只返回原 run，不覆盖发布状态。

## POST /internal/posts/logs/query

请求字段：`actor`、`scope=all`、`page`、`page_size<=100`，可选 `run_date/source_date/account_id/status/material_id/unknown_outcome`。

返回字段仅包含 run/queue/log ID、账号公开标识、素材/剧公开元数据、合规快照、状态、尝试次数、unknown、短链、X 预览、脱敏错误码/错误说明和时间。

分页结构为：

```json
{
  "items": [],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 0,
    "pages": 0
  }
}
```

## POST /internal/posts/runs/query

返回每日批次的 `run_date/source_date/status/expected/queued/published/failed/unknown/started_at/finished_at` 与分页信息。

## GET /api/admin/x-posts/logs

查询参数与 Sidecar 日志查询白名单一致。主后台从 Cookie session 构造 actor，不接受浏览器传入 owner/admin 身份。

## GET /api/admin/x-posts/runs

查询每日批次列表，管理员只读。

## 稳定错误码

- `x_post_material_already_used`
- `x_post_account_day_already_reserved`
- `x_post_daily_run_exists`
- `x_post_daily_candidate_shortage`
- `x_post_retry_requires_review`
- `x_post_unknown_outcome`
- `x_post_rate_limited`
- `x_daily_account_scope_denied`
- `x_daily_scope_invalid`
- `x_post_storage_unavailable`
- `media_preflight_changed`
- `x_post_daily_preflight_failed`
- `x_posts_unavailable`
