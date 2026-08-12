# 042.x-post-manual-scheduled-publish 需求与技术设计

## 背景

X 素材池的“手动发布”弹窗目前只支持立即创建任务并由 `x-post-manual.timer` 领取执行。运营需要在同一弹窗中选择一个北京时间，任务先持久化，到达该时间后才开始现有的整批预检、原子建队列和串行发布。

## 目标

- 手动发布弹窗同时支持“立即发布”和“定时发布”，立即发布保持默认行为。
- 定时任务冻结素材、账号、正文模板、操作者和执行时间；到期前不得被 manual runner 领取或调用 X。
- 到期后复用现有账号、素材/媒体、长视频会员、operator-manual 历史素材复用、自动流程去重、账本和 unknown-outcome 保护。
- 保持旧请求、旧 SQLite 行、自动模板任务和自动排期兼容。

## 范围

### 包含

- 素材池弹窗的发布方式单选、北京时间输入、前端校验、状态展示和低频轮询。
- 手动创建 API 的可选 `publish_mode` / `scheduled_at` 字段。
- `x_post_manual_run` 的增量时间字段、到期领取条件和不可变约束。
- 定时等待期间的素材占用记录；允许选择已在素材池或历史队列中的素材，但等待期间阻止新的素材池加入、自动选择、其他 active 手动/自动模板任务和无关 X 队列抢占。
- 到期、幂等、迁移、并发占用和无真实 X 写入的自动化测试。

### 不包含

- 不增加重复/周期性手动任务。
- 不提供已创建任务的改期、取消或强制提前执行。
- 不改变素材池/短剧池自动排期配置、账号范围或正文模板。
- 不在部署验收中创建定时任务、立即任务或真实 X Post。

## 用户故事 / 业务规则

1. 运营打开手动发布弹窗时，默认选择“立即发布”。
2. 选择“定时发布”后必须输入一个严格晚于服务端当前时间的北京时间，精度为分钟。
3. 服务端是时间有效性的最终裁决者；浏览器时区不能改变所选北京时间。
4. 定时任务创建后返回 HTTP 202，`queued` 表示持久化等待；到期前 `claim_manual_run()` 必须返回 `found=false`。
5. 到期时才刷新账号 token 能力、读取素材源、完成合规/媒体预检并建队列，确保发布使用当时真实状态。
6. 创建任务时先持久化素材占用。operator manual 保留当前生产的显式复用能力，可选择已在素材池或历史队列中的素材且不改写旧记录；从 reservation 建立起，同一素材不能被新加入素材池、被自动流程选择、进入另一个 active 手动/自动模板任务或其他无关 X 队列。
7. 若到期预检失败且尚未生成队列，任务进入 `failed_preflight`，素材占用标记为 `released`，保留审计历史但允许后续重新提交。
8. 成功生成队列后，占用标记为 `consumed`；自动流程继续排除历史 `x_post_queue.material_key`，operator manual 可按当前生产规则显式创建新的独立队列，但任何既有队列均不得自动重试、改写或冒充本次任务。
9. 幂等键必须同时绑定素材、账号、发布方式和定时时间。响应丢失后即使定时时间已到或已过，同一请求仍返回原任务，不重复创建。
10. 旧三字段请求按立即发布处理；旧任务默认 `publish_mode=immediate`、`scheduled_at=''`。
11. `auto_template` 任务仍为立即执行，不允许继承浏览器手动定时参数。
12. 运行中断、限流、明确失败和未知结果继续使用现有停止/核对规则，绝不自动重发。

## 交互与流程

1. 运营输入 1-50 个素材 ID，打开“手动发布”，选择相同数量的可发布账号。
2. 选择“立即发布”或“定时发布”；定时时间显示并解释为 `Asia/Shanghai`。
3. 浏览器二次确认并提交；定时时间参与 sessionStorage 幂等指纹。
4. 后端验证 Cookie、同源 JSON、字段、账号、时间与 active reservation 冲突，在一个 SQLite 事务内创建 run 和 active reservations；已有 pool/历史 queue 不是 operator-manual 创建的拒绝条件。
5. immediate run 可被下一次 15 秒轮询领取；scheduled run 仅在 `scheduled_at <= utc_now()` 时可领取。
6. 到期领取后执行原有整批预检、原子建队列、串行发布和状态聚合。
7. 页面轮询安全 DTO；远离到期时间时降低轮询频率，到期前后恢复快速轮询。

## 技术设计

### 影响模块

| 模块 | 变更 |
| --- | --- |
| `features/x_posts/service.py` | 时间标准化、增量 schema、素材占用、到期 claim、幂等和 DTO 数据 |
| `features/x_accounts/client.py` | 主 API 到 Sidecar 的新字段透传 |
| `features/x_accounts/oauth_service.py` | 内部创建请求和公开白名单 DTO |
| `app.py` | 管理 API 字段兼容、审计时间字段 |
| `static/x-post-material-pool.html` | 发布方式、北京时间输入、状态与轮询交互 |
| `scripts/test_x_post_*.py` | 存储、Sidecar、API、UI、runner 回归 |

### 数据结构

`x_post_manual_run` 增量列：

- `publish_mode TEXT NOT NULL DEFAULT 'immediate'`，仅 `immediate|scheduled`。
- `scheduled_at TEXT NOT NULL DEFAULT ''`，scheduled 时存规范 UTC RFC3339 秒级值；immediate 为空。
- `scheduled_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'`。

新增 `x_post_manual_material_reservation`：

- `id`, `manual_run_id`, `material_key`, `state`, `release_reason`, `created_at`, `updated_at`。
- `state` 仅 `active|consumed|released`；同一 run/material 唯一。
- partial unique index 保证同一 `material_key` 最多一个 active reservation。
- DB trigger 阻止新 pool/无关 queue 绕过 active reservation，允许 reservation 所属 manual run 建自己的队列；operator manual 的 reservation 可叠加在既有 pool/历史 queue 上，auto_template 不继承该例外。
- 占用记录和时间身份不可删除、不可改写，只允许受控状态推进。

### API / 接口

`POST /api/admin/x-posts/material-pool/manual-publish`：

```json
{
  "material_ids": ["123"],
  "account_ids": [7],
  "idempotency_key": "x-post-manual-ui-...",
  "publish_mode": "scheduled",
  "scheduled_at": "2026-08-12T18:30:00+08:00"
}
```

- 旧请求可省略后两个字段，按 `immediate` / 空时间处理。
- scheduled 必须携带带时区的未来时间；UI 固定发送 `+08:00`。
- 返回 DTO 增加 `publish_mode`, `scheduled_at`, `scheduled_timezone`。

`GET /api/admin/x-posts/material-pool/manual-runs/{id}` 返回相同时间字段，不返回 reservation 内部记录。

### 异常与边界

- 缺失/无效/无时区/非分钟精度/已过定时时间：`400 invalid_request`。
- 同一幂等键对应不同方式或时间：`409 x_post_idempotency_conflict`。
- 素材已被另一个 active reservation 占用：`409 x_post_manual_material_unavailable`；既有 pool/历史 queue 对 operator manual 允许显式复用，对自动模板仍拒绝。
- 到期时账号失效、会员降级、素材/合规/媒体变化：整批 `failed_preflight`，零 X 写入。
- SQLite 迁移只新增列、表、索引和触发器，不重建旧表；上线前要求无 active manual run。
- 服务端 UTC 比较使用统一 `Z` 格式，展示固定北京时间；不依赖数据库本地时区。

## 验收标准

1. 弹窗默认立即发布，切换定时后才显示必填北京时间控件和“确认定时发布”。
2. 旧三字段请求仍创建立即任务；新立即请求行为与上线前一致。
3. scheduled 任务到期前连续 claim 均为 `found=false`，队列/发布日志/X Post 计数不变。
4. 到期后同一任务只被领取一次；重复请求或 timer 重跑不重复发布。
5. 等待中的素材无法新加入素材池、进入自动可用候选、创建第二个 active 手动/自动模板任务或进入其他无关队列；若提交前已在 pool/历史 queue 中，原记录保持不变且本次任务仍可到期执行。
6. 到期预检失败会释放 active reservation；建队列后每条新旧队列继续独立遵守 no-retry/unknown 保护，自动流程仍全局排除历史素材，operator manual 的显式复用能力不回退。
7. API/审计/UI 正确显示北京时间，公开 DTO 不泄露 token、内部 bearer 或 reservation 实现细节。
8. schema 迁移连续执行两次结果一致，旧行哈希和历史队列/日志保持不变，`integrity_check=ok`。
9. 本地聚焦与完整 X 发布测试通过，浏览器本地 smoke 通过。
10. 生产部署仅观察 natural `no_pending`/`no_due`，不创建测试任务或真实 X Post。

## 风险与待确认

- 定时时间较远时，账号/素材状态可能变化；设计选择在到期时重新预检并失败关闭。
- 本期不提供取消/改期，运营提交前必须二次确认时间；后续若新增取消，需另行定义 reservation 释放审计。
- 生产 live 基线在部署前并发更新为 `09d267db…`，它在 `46e0720…` 的 Premium relay/repost 与素材池时长变更之上增加 operator-manual pool/历史素材复用；本功能必须基于该 commit 构建，并把同一批准提交的 `service.py` 同步到 Sidecar 与主 API 两处。

## 变更记录

- 2026-08-12：初稿，定义立即/定时双模式、到期领取和素材占用模型。
- 2026-08-12：按实时生产 `09d267db…` 修订，保留 operator-manual 素材复用，并让 active reservation 从创建后开始承担并发隔离。
