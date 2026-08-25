# 056.x-post-pool-refresh-hk-gpu 需求与技术设计

## 背景

X 素材池保留了若干旧规则或旧拓扑产生的错误状态：长视频尚未使用 Premium 中继、`jp`/`ja` 语言迁移、标签限制、违规历史限制，以及旧 GPU 修复/COS 上传失败。部分状态已经不代表当前发布能力，另有标签与语言容量判断仍会再次产生错误。

## 目标

- 长视频按当前同语言 Premium 账号发布后由目标账号 Repost，不因目标账号非 Premium 冻结素材。
- `ja` 素材在日语账号已配置但本批容量已满时保持可用，等待后续排期。
- 标签与历史违规记录均不作为 X 素材发布过滤条件。
- 将 X 媒体修复 worker 和反向隧道迁移到香港 GPU，定向重制失败素材并重试 COS。
- 刷新六类历史状态；删除其余确定无效且无发布关系的素材。

## 范围

### 包含

- 保留处理六类错误：`x_long_video_requires_premium`、`material_language_not_scheduled`、`material_source_tag_unsafe`、`repaired_media_invalid`、`material_has_violation`、`cos_upload_failed`。
- 保留 `drama_not_yet_deliverable` 为“等待可投放时间”，不删除、不清空。
- 删除清单仅限冻结的 3 条：池 ID 86、296、297。
- 香港 GPU 43.154.250.89 的 worker、依赖、COS 配置及受限反向隧道。

### 不包含

- 不创建、不发布任何真实 X Post 或 Repost。
- 不改变 X Token、账号排期、素材加入顺序或已发布审计记录。
- 不删除旧 GPU worker；旧机仅停隧道并作为回滚冷备。

## 用户故事 / 业务规则

1. 超过 140 秒的视频若目标账号不具备长视频资格，必须选择同语言、公开、当前可发布的 Premium 账号作为 relay；没有 relay 才返回 `x_long_video_requires_premium`。
2. “没有该语言账号”与“该语言本批账号已分配满”必须区分；后者不写素材错误。
3. source tag 与 resource tag 只保留为源数据，不参与 X 可用性判断。
4. 历史违规计数保持审计字段兼容，但固定不参与 X 可用性判断。
5. 状态刷新只作用于冻结清单；媒体回填只做下载、探测、修复、COS 校验和池状态记录，不建队列。

## 交互与流程

当前代码与线上只读核验 → 冻结 ID/数量/指纹 → 代码测试 → GitHub 提交 → 三端备份 → 香港 GPU 部署与本机验收 → 隧道原子切换 → 定向媒体回填 → 历史状态事务刷新 → 精确删除 3 条 → 零真实发布验收。

## 技术设计

### 影响模块

- `features/x_posts/selector.py`：移除新旧选择路径的标签 gate。
- `scripts/x_post_schedule_runner.py`：修正语言容量语义，保留 Premium relay 路由。
- `deploy/x-post-media-repair-hk*.service`：香港 GPU worker 与隧道基线。
- `deploy/x-post-media-repair.requirements.txt`：冻结 Python 依赖。

### 数据结构

无 schema 变更。生产写入仅更新 `x_post_material_pool.last_checked_at/last_error_*`，或删除冻结的三条未发布、无队列、无活动占用记录。

### API / 接口

现有内部接口不变：素材池 available/check、Premium relay accounts、GPU `/health` 与 `/internal/x-post-media-repair`。

### 异常与边界

- 任何冻结指纹、数量、状态、队列或占用发生漂移即停止写入。
- 香港 GPU worker、COS HEAD 或 CPU 18820 健康检查失败即恢复旧 GPU 隧道。
- `drama_not_yet_deliverable` 始终排除在清理与状态清空之外。
- 回填失败只更新对应素材为当前错误，不触发发布。

## 验收标准

- 标签素材通过新旧 selector；测试证明不再查询 `resource_tags` 作为 gate。
- 已配置语言容量满时不生成 `material_language_not_scheduled`；真正无该语言账号时仍生成。
- Premium relay 回归通过，线上同语言 relay 可读。
- 香港 GPU 本机 health、鉴权拒绝、NVENC 样例、CPU 18820 health 均通过。
- 6 条 `repaired_media_invalid` 与 2 条 `cos_upload_failed` 完成定向回填或逐条报告当前错误。
- 其余四类历史状态经当前源数据/账号能力确认后清空；三条删除完成；五条未来可投放记录保持 deferred。
- 发布队列与日志总数不因本次验收增加。

## 风险与待确认

无待用户确认项。用户已明确授权迁移、重制、重试、状态刷新及删除其他错误素材；仍执行最小范围和可回滚保护。

## 变更记录

- 2026-08-25：创建需求、冻结线上范围并完成代码修复设计。
