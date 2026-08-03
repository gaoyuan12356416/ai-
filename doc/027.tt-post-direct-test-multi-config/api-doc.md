# API 合同（027 基线已上线；BUG-005 增量已实现、待部署）

## 通用约定

- 管理端前缀：`/api/admin/tt-posts`。
- 写接口拒绝未知字段；时间使用 UTC ISO-8601，排期时区固定为 `Asia/Shanghai`。
- 错误响应只返回安全的 `error/code` 与 `message`，不返回 Token、claim token、上游原文或 Secret。
- 新页面的“立即测试”只调用 `/test-publish`。旧 `/run-now` 仍保留为兼容自动池手工触发接口。

## 路由总览

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/accounts` | 账号列表、自动成员状态和当前配置 |
| GET | `/auto-config` | 读取原子自动发布配置 |
| POST | `/auto-config` | 原子保存描述、开关/时间和账号集合 |
| POST | `/materials/preview` | 校验单个素材并返回发布投影 |
| GET | `/material-pool` | 素材池列表与发布投影 |
| POST | `/material-pool` | 单素材加入自动池 |
| POST | `/test-publish` | 创建独立立即测试任务 |
| GET | `/direct-tests` | 分页/筛选查询立即测试任务 |
| GET | `/tasks` | 只读统一查询自动/排期与立即测试 |
| POST | `/run-now` | 旧自动池手工触发兼容接口；UI 不调用 |

管理端没有 `/direct-tests/{id}` 详情接口，也没有人工 reconcile POST。

## 1. 账号与自动成员状态

`GET /api/admin/tt-posts/accounts`

```json
{
  "items": [
    {
      "source_account_id": "640",
      "account_name": "Dramawave popular reels",
      "auto_publish_selected": true,
      "auto_publish_state": "active",
      "auto_publish_config_version": 8
    }
  ],
  "account_source_available": true,
  "auto_publish_config": {
    "version": 8,
    "enabled": true,
    "publish_times": ["11:00"],
    "account_ids": ["640"]
  },
  "gates": {}
}
```

`auto_publish_state` 的精确枚举：

- `active`：已选中且自动发布启用，账号当前可用；
- `paused`：已选中，但总开关关闭；
- `attention_required`：已选中，但账号源/账号当前需要处理；
- `not_selected`：未加入自动发布配置。

## 2. 读取自动发布配置

`GET /api/admin/tt-posts/auto-config`

响应顶层字段是 `item`，不是 `config`：

```json
{
  "item": {
    "version": 8,
    "enabled": true,
    "timezone": "Asia/Shanghai",
    "publish_times": ["11:00"],
    "account_ids": ["640", "641"],
    "caption_template": "Drama ID: {{content_id}}\n{url}\n{desc}",
    "user_consent": true,
    "consent_version": "tt-post-consent-v2",
    "consented_at_utc": "2026-08-03T04:02:00Z",
    "legacy_review_required": false,
    "legacy_schedule_mode": "atomic",
    "legacy_publish_times_by_account": {},
    "legacy_membership_mode": "atomic"
  },
  "gates": {}
}
```

单例尚未保存时返回只读 version-0 投影，不写数据库。若旧账号的完整时间元组不同：

```json
{
  "item": {
    "version": 0,
    "enabled": true,
    "publish_times": [],
    "account_ids": ["640", "641"],
    "legacy_review_required": true,
    "legacy_schedule_mode": "mixed",
    "legacy_publish_times_by_account": {
      "640": ["11:00"],
      "641": ["11:10"]
    }
  }
}
```

## 3. 原子保存自动发布配置

`POST /api/admin/tt-posts/auto-config`

只接受以下字段；除 `consent` 外均必填：

```json
{
  "expected_version": 8,
  "enabled": true,
  "timezone": "Asia/Shanghai",
  "publish_times": ["11:00"],
  "source_account_ids": ["640", "641"],
  "caption_template": "Drama ID: {{content_id}}\n{url}\n{desc}",
  "consent": {
    "accepted": true,
    "version": "tt-post-consent-v2",
    "accepted_at": "2026-08-03T04:02:00Z"
  }
}
```

- `publish_times` 在当前版本最多一个值；账号最多 50 个。
- `enabled=true` 要求非空账号列表、有效 consent，并实时校验全部成员的设置/creator-info。
- `enabled=false` 不要求新 consent。UI 可发送 `accepted=false` 占位；服务端忽略它并由 core 保留已有 consent。
- 关闭态可无远端依赖地保留/移除已有成员；若新增成员，新 ID 必须出现在当前可信账号快照且已有本地发布设置，否则整次保存 0 写入。
- mixed legacy 首次保存必须显式提交统一时间且 `enabled=false`；保存成功后下一版本才可启用。
- 成功响应为 `{"item": saved, "gates": ...}`；core 在同一事务写单例配置并同步/禁用兼容 schedule。

## 4. 素材校验与发布投影

`POST /api/admin/tt-posts/materials/preview`

```json
{"material_id":"5837129"}
```

响应的发布字段直接合并到 `item`，不是嵌套对象：

```json
{
  "item": {
    "material_id": "5837129",
    "content_id": "ico5tD77Pb",
    "status": "validated",
    "publication_state": "published",
    "publication_status": "published",
    "publish_count": 1,
    "unknown_count": 1,
    "attempt_count": 2,
    "latest_published_at_utc": "2026-08-01T03:10:00Z",
    "latest_publish_id": "7668...",
    "latest_publish_url": "https://www.tiktok.com/...",
    "latest_status_at_utc": "2026-08-03T03:10:00Z"
  },
  "gates": {}
}
```

状态只有 `published|unknown|unpublished`。有任何 published 时主状态为 published；否则有 unknown/`publishing|reconciling` 时为 unknown；仅有 queued/preparing/ready/failed/canceled 时为 unpublished。自动池 `consumed` 不参与判断。

发布投影是展示事实，不等于永久 auto eligibility：仅由 direct-test 产生的 published 不修改 pool；同素材 direct-test 到达明确终态后，available pool 仍可正常领取。

`GET /material-pool` 的每个 item 合并相同字段；summary 使用 `published`、`unpublished`、`unknown_publication`。

## 5. 单素材加入自动池

`POST /api/admin/tt-posts/material-pool`

新 UI 每个素材调用一次：

```json
{
  "idempotency_key": "tt-post:intake:640:5837129:...",
  "source_account_id": "640",
  "material_id": "5837129",
  "content_id": "ico5tD77Pb",
  "expected_config_version": 8,
  "caption_template": "Drama ID: {{content_id}}\n{url}\n{desc}",
  "consent": {
    "accepted": true,
    "version": "tt-post-consent-v2",
    "accepted_at": "2026-08-03T04:05:00Z"
  }
}
```

- `source_account_id` 必须属于 version 8 的 `account_ids`。
- 服务端使用 version 8 的保存模板；若兼容字段 `caption_template`/`caption_text` 存在，必须与服务端渲染结果一致。
- 接口不接受 `material_ids` 或 `pool_account_id` 批量合同。

## 6. 创建独立立即测试

`POST /api/admin/tt-posts/test-publish`

请求字段必须精确为：

```json
{
  "source_account_id": "777",
  "material_id": "5837129",
  "expected_config_version": 8,
  "idempotency_key": "tt-post:direct-test:777:5837129:...",
  "consent": {
    "accepted": true,
    "version": "tt-post-direct-test-v1",
    "accepted_at": "2026-08-03T04:06:00Z"
  }
}
```

合同约束：

- 目标账号是独立显式单选，可以不属于自动配置 `account_ids`；自动开关也可以关闭。
- `expected_config_version` 必须等于当前已保存且大于 0 的版本，只用于读取并冻结描述模板。
- 目标账号仍必须存在、有已保存发布设置、实时 creator-info 兼容、隐私为 `PUBLIC_TO_EVERYONE`、`allow_comment=true`，且正式门禁开放。
- 素材可未入池，也可历史已发布；服务端重新解析素材并创建新的 `tttest-*` GPU job。
- 同素材已有活动/unknown direct-test 或活动/unknown legacy queue 时拒绝；不同素材不因同账号已有任务而在创建层误拒绝，发布 claim 仍保持账号串行。
- 不读取、领取或修改自动素材池，也不创建 legacy queue/run。
- direct-test 到达 `published|failed|canceled` 明确终态后不永久阻断该素材的 auto claim；若 pool 原本 available，后续仍按原账号/FIFO 正常消费。

成功/同键重放均返回 200 形状：

```json
{
  "item": {
    "id": 117,
    "direct_test_id": 117,
    "material_id": "5837129",
    "account_id": "777",
    "source_account_id": "777",
    "config_version": 8,
    "gpu_job_id": "tttest-...",
    "status": "queued",
    "preparation_status": "queued",
    "publication_status": "unpublished",
    "publish_ready": false,
    "task_type": "direct_test"
  },
  "preparation_wakeup_requested": true,
  "preparation_timer_fallback_seconds": 60,
  "gates": {}
}
```

同键同账号/素材/版本/consent version/consent accepted_at 返回原任务；同键改变任一事实返回 `tt_post_direct_test_idempotency_conflict`。精确重放不再调用门禁、账号源、素材源或 TikTok 网络依赖。

客户端必须让 key 贯穿全部非终态：`queued|preparing|ready|publishing|reconciling|unknown` 都不能清除，并要连同原 config version/consent accepted_at 保存。只有任务明确 `published|failed|canceled` 后，用户显式发起“再测试一次”才生成新 key。

## 7. 查询测试任务

`GET /api/admin/tt-posts/direct-tests?page=1&page_size=20&source_account_id=777&material_id=5837129&status=unknown`

`page_size` 最大 100。响应：

```json
{
  "items": [],
  "pagination": {"page": 1, "page_size": 20, "has_more": false},
  "gates": {}
}
```

任务 API item 会移除 claim token 等敏感字段，并补充 `direct_test_id`、`source_account_id`、`caption_text`、`duration_sec`、`preparation_status`、`publication_status`、`publish_ready` 和 `task_type`。

## 7A. 统一发布任务只读列表（BUG-005）

`GET /api/admin/tt-posts/tasks?page=1&page_size=20&task_type=all&source_account_id=640&material_id=5837129&status=published`

查询参数：

- `page>=1`，`1<=page_size<=100`；
- `task_type=all|automatic|direct_test`，省略时为 `all`；
- `source_account_id`、`material_id`、`status` 可选，并同时作用于两类任务；
- 未声明参数或非法类型返回 400，且查询不得产生业务写入。

服务端必须在同一 SQLite 读快照中先读取并合成 queue/direct-test，再执行筛选、summary、稳定排序和分页。禁止由浏览器分别读取两个已经分页的接口后拼接。统一排序按 `task_at_utc DESC`，再按 `task_type`、`task_id DESC` 稳定打破并列；automatic 的 `task_at_utc` 为排期时间，direct-test 为创建时间。旧 `/queue` 自身排序不变。

响应示例：

```json
{
  "items": [
    {
      "task_key": "direct_test:17",
      "task_type": "direct_test",
      "task_label": "立即测试",
      "task_id": 17,
      "direct_test_id": 17,
      "source_account_id": "640",
      "material_id": "5837129",
      "content_id": "ico5tD77Pb",
      "caption_text": "...",
      "status": "published",
      "raw_status": "published",
      "status_group": "published",
      "task_at_utc": "2026-08-03T09:43:14Z",
      "created_at": "2026-08-03T09:43:14Z",
      "updated_at": "2026-08-03T09:44:25Z"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 1
  },
  "summary": {
    "total": 1,
    "scheduled": 0,
    "processing": 0,
    "needs_review": 0,
    "published": 1
  },
  "gates": {}
}
```

类型与操作合同：

- automatic：`task_key=automatic:<queue_id>`、`task_label=自动/排期发布`，保留既有 queue 字段及 `queue_id`。
- direct-test：`task_key=direct_test:<direct_test_id>`、`task_label=立即测试`；返回已冻结 caption、账号设置、GPU job、状态、创建/更新时间和安全错误。页面按 `task_type` 将该行限制为只读详情，不生成 queue 操作。
- `task_id` 仅用于显示与稳定排序。调用事件或写操作时只能使用类型对应的源 ID；direct-test ID 绝不能作为 queue ID。

summary 口径：

- `scheduled`：queue `scheduled`；
- `processing`：queue `claimed|publishing|reconciling`，以及 direct-test `queued|preparing|ready|publishing|reconciling`；
- `needs_review`：queue `unknown`/`unknown_outcome`，以及 direct-test `unknown`；
- `published`：两类任务 `published`；
- 统计按任务行计算，不按 material ID 去重，并基于过滤后、分页前全集。

该接口是纯只读管理投影：不得修改 queue/direct-test/pool/run/config，唤醒 runner，调用 GPU/COS/TikTok，或创建任何 Post。

## 8. 内部 reconciliation

- `GET /internal/tt-posts/direct-tests/reconciling?limit=100`：列出待内部核对任务。
- `POST /internal/tt-posts/direct-tests/{id}/reconcile`：由服务端基于 GPU ledger/远端状态核对；不接受运营提交 `resolution/publish_id/evidence_ref` 的管理端合同。

## 9. 同分钟 due

`POST /internal/tt-posts/schedules/due`

请求只允许：

```json
{"limit":1}
```

服务顺序：

1. 收集宽限期内所有启用 legacy schedule 的 due slots；
2. 门禁开放时，按顺序为每个 slot 调用现有 `claim_recurring_run`；每个调用独立原子创建 run 并预留精确 FIFO 素材；
3. 所有当前 slot 的预占尝试结束后，才处理已有 claimed/unbound recovery；因此 recovery 进入 `_execute_recurring_run` 也不能抢在本轮 preclaim 之前调用 creator-info；
4. 再执行本轮新预占 run；
5. `limit` 只限制 recovery/执行/返回 items，不截断步骤 2；无素材的 slot 返回 skipped 且不创建空 run。

响应没有 `due_batch/persisted_count/existing_count`：

```json
{
  "items": [],
  "current_shanghai_minute": "2026-08-04 11:00",
  "grace_seconds": 600,
  "deferred_count": 49,
  "oldest_deferred_at_utc": "2026-08-04T03:00:00Z",
  "gates": {}
}
```

实现不新增 `tt_post_auto_due`；稳定 run key、现有 run 唯一约束和 pool reservation 提供去重与恢复。

## 10. 关键错误码

| HTTP | code | 场景 |
| --- | --- | --- |
| 400 | `invalid_request` | 字段集合、类型或分页错误 |
| 400/409 | `tt_post_consent_required` | 启用/测试缺少有效确认；关闭不触发 |
| 409 | `tt_post_auto_config_version_required` | 立即测试尚无已保存版本 |
| 409 | `tt_post_auto_config_version_conflict` | expected version 已过期 |
| 409 | `tt_post_auto_config_legacy_review_required` | mixed legacy 尚未按两步迁移 |
| 409 | `tt_post_auto_account_not_found` | 关闭态新增成员不在可信账号快照 |
| 409 | `tt_post_auto_account_not_selected` | 自动素材入池账号不属于配置；不用于立即测试 |
| 409 | `tt_post_live_gates_closed` | 正式发布门禁关闭 |
| 409 | `tt_post_direct_test_public_comment_required` | 测试账号非所有人可见或未允许评论 |
| 409 | `tt_post_direct_test_idempotency_conflict` | 同 key 改变请求事实 |
| 409 | `tt_post_direct_test_active` | 同素材有活动/unknown 测试 |
| 409 | `tt_post_material_publish_active` | 同素材有活动/unknown legacy queue |

## 兼容性

1. 新 schema 只增加 `tt_post_auto_publish_config` 与 `tt_post_direct_test`；不新增 direct-test event 或 auto-due 表，既有 `tt_post_event` 不变。
2. `tt_post_account_setting`、GPU prepare/publish 合同、GPU ledger 格式、legacy pool/queue material 唯一约束保持。
3. 单例未保存时只读投影旧 schedule；首次保存同步 legacy schedule，便于旧 release 继续读取。
4. 旧 `/run-now` 保留兼容语义；独立测试的唯一页面入口是 `/test-publish`。
5. BUG-005 新增 `/tasks` 只读合成视图；旧 `/queue` 的 items、summary、pagination、排序和操作语义保持不变。
6. 统一视图不新增 `tt_post_direct_test_event` 或 direct-test 管理写路由；direct-test 行不能调用 queue event/cancel/reconcile。
