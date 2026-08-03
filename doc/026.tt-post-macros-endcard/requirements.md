# 026.tt-post-macros-endcard 需求与技术设计

## 文档状态

- 日期：2026-08-03
- 状态：需求冻结，代码已实现，最终复核、部署与环境验收待回填
- 发布限制：本轮已授权生产部署和 `prepare-only` 验收；禁止创建真实 TikTok Post、调用发布 canary、保存或人为触发生产自动排期，现有自动发布配置必须原样保留
- 相关基线：保留现有 TT 素材池、手动立即发布、每日排期及账号发布设置能力；本需求不能破坏既有 2c 控制面修复

## 背景

TT 素材池现有描述模板可渲染 Drama ID，URL 宏分支已引入 TT 专用短链，但仍缺少两项正式发布能力：

1. 模板需要支持来自素材真实业务数据的 `{desc}`，并保证描述在入池后不因源库变化而漂移。
2. 现有 `direct_clean` 是正式 Direct Post 可用模式，但明确不添加片尾；`branded_preview` 会制作片尾，却不是正式 Direct Post 可用模式。需要新增独立的 `direct_outro`，制作固定已审核片尾并具备正式直发资格，同时保持 `direct_clean` 行为不变。

此外，最终 caption 必须符合 TikTok 的 2200 UTF-16 code unit 上限；所有错误均应在入池、排队或制作阶段 fail closed，不能静默截断或带未解析宏发布。

## 目标

- 支持精确小写、单花括号宏 `{url}` 和 `{desc}`；保留 `{{contect_id}}`、`{{content_id}}` 两个既有 Drama ID 别名。
- `{url}` 渲染为 TT 专属域名下的 19 位 `8` 开头短链，短链目标为固定 W2A 地址，参数顺序和来源可审计。
- `{desc}` 的唯一事实来源为与素材匹配的 `ads_drama_resource.desc`，由后端解析、清洗并在 intake 时冻结，随后原样贯穿 recurring pool 和 queue。
- 宏渲染严格单次、非递归；描述文本中的 `{url}`、`{desc}` 或 Drama ID 宏样式字符只能作为普通文本保留。
- 最终 caption 为空或超过 2200 UTF-16 code units 时拒绝入池/排队，绝不静默截断。
- 新增 `direct_outro`：正式 Direct Post eligible、复用既有已审核的 Logo/tutorial-outro 合成器、独立媒体 profile、完整资产指纹与复用契约。
- 保持 `direct_clean` 不添加 logo/片尾、既有 profile 和正式发布资格完全不变；保持 `branded_preview` 仍为需审核且不可正式直发。
- 本轮通过 `prepare-only` 证明成片、片尾、指纹、时长和 URL 身份；不创建真实 TT Post。

## 范围

### 包含

- TT caption 模板解析、校验、渲染和幂等比对。
- `ads_drama_resource.desc -> material_intake -> recurring_material_pool -> publish_queue` 的冻结链路。
- TT 专用短链生成、落盘和 Nginx 路由兼容性。
- CPU API、SQLite additive migration、素材池 UI 提示及逐素材预览校验。
- GPU `direct_outro` 媒体模式、profile、片尾资产指纹、成片 manifest 和 Direct Post eligibility。
- URL 与源素材地址不相等的强校验、prepare-only 证据和双轨回滚。
- 既有自动排期开关、版本冲突、素材状态统计、立即发布门禁和账号降级展示的回归。

### 不包含

- 本轮创建任何真实公开、私密或测试 TikTok Post。
- 调用 `/internal/tt-post/publish`、`/internal/tt-post/canary-publish`、`/api/admin/tt-posts/run-now`，或人为触发生产排期 runner。
- 修改已有 TikTok Post 的可见性、评论、Duet、Stitch 或商业披露状态。
- 更改其他业务的 COS；TT 制作结果只能使用 TT 专用桶，其他场合继续使用原桶。
- 重写历史已发布 queue caption，或用当前源库 description 覆盖已经冻结的数据。
- 改变 `direct_clean`、`branded_preview` 的既有媒体语义。

## 术语与事实来源

| 名称 | 定义 |
| --- | --- |
| source description | 素材通过 `content_id + language` 唯一解析到的 `ads_drama_resource.desc` |
| frozen description | 入 intake 时由后端清洗并保存的 description；后续发布只使用此值 |
| final caption | queue 中宏全部解析后的 `caption` / `caption_text` |
| UTF-16 长度 | `len(text.encode("utf-16-le")) / 2`，emoji 等补充平面字符计 2 个 code units |
| `direct_outro` | 仅添加固定已审核片尾、可用于正式 Direct Post 的新媒体模式 |
| prepare-only | 只调用制作接口并核验输出；不建 queue、不发布、不触发 schedule |

## 用户故事 / 业务规则

### R1. 宏语法

1. 允许的宏只有精确 token：`{url}`、`{desc}`、`{{contect_id}}`、`{{content_id}}`。
2. `{URL}`、`{DESC}`、`{{url}}`、`{{desc}}`、`{ url }` 等大小写、括号数量或空格不同的写法必须拒绝，不能当作普通可发布文本放行。
3. 模板必须至少包含一个既有 Drama ID 宏；同一宏可重复出现，两个 Drama ID 别名保持向后兼容。
4. 渲染必须是一次 token 扫描或等价的一次替换，不能先替换 `{desc}` 后再次扫描结果。因此 description 内含 `{url}`、`{desc}`、`{{content_id}}` 时保留字面值。
5. 无宏的历史合法模板继续按原行为工作。

### R2. `{desc}` 来源、清洗与冻结

1. UI 和客户端不能提交 description 作为事实来源；素材 preview 与入池都由后端重新解析素材。
2. 唯一来源是 `ads_drama_resource.desc`。不得使用素材名、剧名、AI 生成描述、页面传值或 queue 创建时的临时源库查询代替。
3. 后端在 intake 时清洗 description：统一连续空白为单个空格、去掉首尾空白、拒绝空值、拒绝不可发布控制字符，源字段读取上限为 4096 字符；不对最终 caption 做截断。
4. 冻结值必须从 material intake 复制到 recurring pool，再复制到 queue。排期、重试、幂等回放和发布不得重新查询 `ads_drama_resource`。
5. 源库 desc 在入池后发生变化时，已存在 intake/pool/queue 的最终 caption 不得变化。
6. 老库通过 additive migration 增加 description 字段；历史已发布 queue 不重写。未冻结 description 且模板含 `{desc}` 的可用老 pool 必须先受控回填并重算，或重新入池，不能发布字面量 `{desc}`。

### R3. `{url}` 与 TT 专用短链

1. 每个需要 `{url}` 的 queue 使用专属 19 位十进制 ID，首位固定为 `8`；ID 与 queue 唯一绑定。
2. 短链格式为 `https://gy.g2flow.com/s2l/8xxxxxxxxxxxxxxxxxx.html`。
3. 短链基址固定为 `https://gy.g2flow.com/s2l`；wrapper 的长链目标固定为 `https://www.dramawavew2a.com/ads/101/2250/view`，并按以下顺序生成参数：`c`、`af_adset`、`af_adset_id`、`af_ad`、`af_ad_id`、`af_channel=AIpost`、`af_c_id`、`af_dp`。
4. wrapper 必须原子写入且同 ID 内容不可变；重复同一 queue/idempotency key 复用相同短链，冲突内容返回 409。
5. TT 精确 Nginx 路由必须位于 X 的通用数字短链路由之前，不能改变已有 X 链接。
6. 缺少生成长链所需的冻结素材元数据时 fail closed，不能生成不完整或猜测参数的链接。

### R4. caption 完整性

1. 最终 caption 只有在 queue 拥有短链、frozen description 和 content ID 后才完成渲染；GPU 永远只接收完全渲染后的 caption，不解释任何宏。
2. 最终 caption 必须非空，且不超过 2200 UTF-16 code units；边界 2200 允许，2201 拒绝。
3. 不得使用 Python code point 数量或 UTF-8 byte 数量替代 UTF-16 校验。
4. 校验失败不消费素材、不绑定 queue、不创建 wrapper、不触发 runner。
5. 幂等回放应比较冻结模板、冻结 description、短链和最终 caption；同 key 不同事实数据返回 409。

### R5. `direct_outro`

1. 新增精确媒体模式 `direct_outro`，配置加载、健康检查、manifest 和 API 响应均返回该名称。
2. 建议 profile：HEVC 为 `tt-post-direct-outro-hevc-720x1280-v1`，H.264 为 `tt-post-direct-outro-h264-720x1280-v1`。CPU 的 expected profile 与 GPU 实际 profile 必须完全一致。
3. `direct_outro` 复用既有 `branded_preview` 已审核的 Logo/tutorial-outro 合成器：固定片尾资产、圆角 Logo、Drama ID/教程文字格式和 `phone-match-0.9s` transition 均沿用现有合同；不得额外引入未审核视觉元素。它与 `branded_preview` 的差异只在独立 mode/profile、manifest 版本和正式 Direct Post eligibility。
4. 制作流程为：下载并校验源视频 -> 按配置裁掉源片尾 -> 以既有审核合同规范化 Logo/tutorial outro -> 使用固定 transition 合成 -> 探测与校验 -> 上传 TT 专用 COS。
5. 期望成片时长为 `source_duration - trim_tail + outro_duration - transition_overlap`，允许探测容差必须沿用 worker 的既有媒体校验阈值。
6. manifest/reuse contract 至少冻结 `media_mode`、`profile`、`source_url_sha256`、实际 `source_sha256/source_size`、`outro_sha256/outro_size`、`logo_sha256/logo_size`、`source_trim_tail_seconds` 和 `transition`。
7. 固定片尾文件或 profile 变化后不得错误复用旧成片；相同 job ID 但契约变化返回 `prepare_idempotency_conflict`。
8. `direct_outro` 的 `direct_post_eligible=true` 仅表示媒体可进入正式 Direct Post 流程，不得绕过账号 creator-info、用户 consent、隐私/评论设置、AIGC、商业披露、全局 gate、队列 claim 和幂等门禁。
9. `prepared_media_url` 必须与 `source_media_url` 不同；相同则以 `tt_prepared_media_matches_source`（或等价稳定错误码）拒绝。
10. `direct_clean` 保持只规范化源素材、无片尾、无 logo、既有 profile 和正式发布资格不变。`branded_preview` 保持需品牌预览审核且不可正式直发。

### R6. 本轮安全边界

1. 验收 job 必须使用新的唯一 job ID 和不可变源 URL；若上游可提供 source SHA/size，必须一并传入并校验。
2. 只允许调用 `/internal/tt-post/prepare` 或通过隔离的 intake preparation 流程制作；不得调用任何 publish/canary/run-now/schedule-save 接口。
3. 验收前后记录 queue 数、发布 ledger 数、自动排期版本/启用状态，证明没有创建 Post 或改变排期。
4. 验收素材上传 TT 专用 COS；专用桶和域名不得被其他业务复用。

## 交互与流程

### 素材入池

1. 管理员选择账号，输入素材 ID 和 caption template。
2. 页面调用 preview；后端解析真实 `content_id`、language、`ads_drama_resource.desc` 和短链所需元数据。
3. 页面用每条素材的真实 description 做单次渲染预览，展示 UTF-16 使用量和错误；页面不把 description 放入写请求。
4. 用户确认后提交入池；后端再次解析并冻结事实数据，生成 material intake。
5. 后台 worker 仅制作 `direct_outro` 成片。准备完成后，冻结数据连同成片元数据进入 recurring pool。

### 排期到 queue

1. 自动或手动 run 只选择 `preparation_status=ready` 且 `status=available` 的 pool item。
2. 后端从 pool 冻结 description/素材元数据到 queue。
3. 若模板含 `{url}`，生成并原子写入短链 wrapper。
4. 后端一次性渲染 content ID、description 和 URL，执行 UTF-16 校验后才绑定 queue。
5. GPU 发布端只接收最终 caption 与已制作成片 URL；不访问业务库、不解析宏。

### prepare-only 验收

1. 固定 gate 和排期基线，创建全新 prepare job。
2. 调用 prepare，核对 `media_mode=direct_outro`、独立 profile、输出 hash/size/duration、片尾与 Logo hash/size、`direct_post_eligible=true`。
3. 下载成片，用 ffprobe/抽帧/人工观看确认源内容、固定片尾、声音、转场和时长。
4. 证明输出 URL 不等于源 URL，且属于 TT 专用 COS 域名。
5. 再次读取 queue、publish ledger、schedule 基线；数量和版本不得变化。

## 技术设计

### 影响模块

| 模块 | 变更 |
| --- | --- |
| `features/tt_posts/core.py` | 宏 tokenizer/单次渲染、UTF-16 校验、SQLite schema/migration、intake/pool/queue 冻结与幂等 |
| `features/tt_posts/service.py` | resolver description 传递、入池信任边界、pool->queue 复制、URL 完成渲染、prepared/source URL 身份校验 |
| `features/tt_posts/links.py` | TT 19 位短链、W2A 参数、原子 immutable wrapper |
| `features/tt_gpu/worker.py` | `direct_outro` 配置/profile、固定片尾制作、manifest/reuse、eligibility 和健康检查 |
| `static/tt-post-pool.html` | 参考 X 素材池 UI 的模板编辑、宏帮助、逐素材预览、UTF-16 错误和 schedule 控制回归 |
| `scripts/test_tt_*.py` | core/service/link/UI/GPU/runner 合同测试 |
| `deploy/*` | CPU/GPU profile、TT COS、短链目录和 Nginx 精确路由 |

注意：`core.py`、`service.py`、`tt-post-pool.html` 及三份对应测试同时承载既有 2c 修复和宏分支，合并时必须做语义评审，不能仅以“无文本冲突”判断安全。

### 数据结构

| 表/制品 | 字段 | 规则 |
| --- | --- | --- |
| material intake | `description TEXT NOT NULL DEFAULT ''` | 已有/迁移后作为首次冻结点；请求 hash 包含 description |
| recurring pool | `description TEXT NOT NULL DEFAULT ''` | 只从对应 intake 复制；运行时不查源库 |
| queue | `description TEXT NOT NULL DEFAULT ''` | 只从选中的 pool/intake 复制；caption 与幂等比较使用该值 |
| queue | `material_name`、`drama_name`、`material_language`、`material_tag` | `{url}` 长链的冻结元数据 |
| queue | `short_link_id`、`short_url`、`long_url` | `short_link_id > 0` 使用 partial unique index |
| prepare manifest | `media_mode/profile/source/outro/transition` 指纹 | 决定是否可安全复用成片 |

所有新增 SQLite 列使用 additive migration 和非空默认值，不删除表、不重建历史 queue。空默认值只用于兼容读取；模板含 `{desc}` 时不得把空默认值当作可发布 description。

### API / 接口

- 不新增公开管理端路由；扩展现有 `/api/admin/tt-posts/materials/preview`、`/api/admin/tt-posts/material-pool` 和 queue 响应字段。
- 保持 `/api/admin/tt-posts/schedule` 与 `/api/admin/tt-posts/run-now` 路由和权限不变；本轮验收不调用写接口。
- CPU 后台制作继续使用 `/internal/tt-posts/preparations/{id}/process`。
- GPU 制作继续使用 `POST /internal/tt-post/prepare`；mode 由 GPU 部署配置固定，不接受客户端随请求切换。
- GPU publish/canary 路由不变，本轮禁止调用。

详见 `api-doc.md`。

### 异常与边界

| 情况 | 结果 |
| --- | --- |
| description 解析为空、冲突或无唯一素材映射 | 400/409，拒绝入池 |
| 模板出现未知或大小写错误宏 | 400 `invalid_caption_template` 或等价稳定错误 |
| final caption 为空或 >2200 UTF-16 | 400，拒绝且不截断 |
| 老 pool 模板含 `{desc}` 但 description 为空 | 409，要求回填/重新入池 |
| 短链元数据缺失、wrapper 已存在但内容不同 | fail closed；冲突返回 409 |
| CPU expected profile 与 GPU profile 不同 | 409 `tt_prepared_media_profile_mismatch` |
| prepared URL 等于 source URL | 409 `tt_prepared_media_matches_source` |
| 相同 GPU job ID 的 mode/profile/片尾/源契约变化 | 409 `prepare_idempotency_conflict` |
| 固定片尾缺失、hash 失败或时长不合法 | GPU prepare 失败，不进入 ready pool |
| 2200 边界含 emoji | 按 UTF-16 code units 计算，不能按字符数误放行 |

## 验收标准

1. M/F/U/D/I/S 六组 42 条用例全部通过，P0/P1 无开放缺陷。
2. `{desc}` 可追溯到 `ads_drama_resource.desc`，在 intake/pool/queue 三层值一致；源库变更不影响冻结值。
3. 合并模板中 content ID、description 和 TT 短链只替换一次；描述内宏样式文本保持字面值。
4. 2200 UTF-16 正边界通过，2201 失败；没有任何静默截断。
5. `direct_outro` 成片符合既有 Logo/tutorial-outro 合同，使用独立 profile 和完整 asset/source fingerprint，并报告 `direct_post_eligible=true`。
6. `direct_clean` 无片尾回归测试继续通过；`branded_preview` 仍不可正式直发。
7. prepare-only 输出 URL 与源 URL 不同，属于 TT 专用 COS；下载后的 hash/size 与响应/manifest 一致。
8. 验收前后真实 Post、queue/publish ledger 和生产 schedule 状态均无变化。
9. CPU 宏/短链和 GPU `direct_outro` 可独立回滚，回滚演练不破坏旧数据和既有 X 短链。

## 风险与待确认

- 同一 source URL 背后的内容若可变，而 job ID 只含 URL hash，可能错误复用旧成片。正式上线前必须确认 URL 不可变，或让调用方传并冻结 source SHA/size。
- `direct_outro` 片尾与 Logo 资产必须有明确审核版本、SHA-256、时长/尺寸和回滚副本；不能依赖“同路径文件未变化”的假设。
- 历史 available pool 中 description 为空的迁移策略必须先盘点，禁止一刀切用当前源库覆盖历史已发布记录。
- Nginx TT 精确路由与既有 X `s2l` 通用数字路由有匹配顺序风险，必须用两类真实形状 URL 做无缓存验证。
- CPU/GPU profile 切换不一致会让所有 prepare fail closed；上线需同一变更窗口并保留旧 profile 回退值。
- TikTok 正式发布可见性与评论能力仍由账号实时 creator-info 和已保存设置约束；prepare-only 不能替代一次经授权的最终生产发布验收。

## 变更记录

| 日期 | 版本 | 说明 |
| --- | --- | --- |
| 2026-08-03 | v1 | 冻结 `{url}`、`{desc}`、UTF-16 和 `direct_outro` 需求；明确本轮 prepare-only、不创建真实 Post |
