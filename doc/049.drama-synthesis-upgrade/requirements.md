# 剧集合成：香港 GPU、随机模板、短链与 YouTube 发布

## 最新覆盖决定：使用现有授权与现有数据库账号（2026-08-27 16:35）

用户明确取消专用数据库账号隔离要求。按 [现行发布合同](ads-ai-new-tables-20260827.md) 使用 CPU 现有 ads_aius 和已有 YouTube OAuth，发布结果只写 ads_ai 三张新表；不创建/修改账号、不动原 MySQL 表。RPC v3 如实声明共享账号与应用表白名单，保留秘密保护、无 trigger/FK 检查、幂等与未知结果停止。无需再提供管理员凭据。以下专用账号/1410/旧库迁移内容均是历史，不再作为上线门禁；当前部署及真实测试尚待完成，最终实机状态另行记录。

## 目标与边界

2026-08-27 最新确认：YouTube 结果改为在 `ads_ai` 新建专用三表，不写、不改、不复制原表；原库仅保留频道/OAuth 只读查询。旧统一表写入、migrator、旧备份门禁被 [新表合同](ads-ai-new-tables-20260827.md) 取代；其他附件要求不变。

- CPU `43.166.187.96` 负责全部业务查询与协调：剧集/分集/素材地址、模板目录与配方冻结、任务状态、频道/OAuth、短链、YouTube 发布/评论和统一表同步；页面、队列、SQLite 均留 CPU。香港 GPU `43.154.250.89` 只接收完整制作参数，下载素材、制作、上传 COS，再把结果返回 CPU；不持有业务数据库或 YouTube 凭据，不以缺参为由回查 CPU/数据库。
- 保持现有视觉、侧栏、表单、卡片和表格。输出键精确为 `concat_video`、`no_bgm_video`、`cover_16x9`、`random_template_video`；所有复选框新建时默认不勾选，服务端拒绝零输出。
- 不改香港 GPU 现有 `ads_video_producer.service`；不静默双写或自动回退旧 GPU。
- 已在现有 `gy.g2flow.com` 站点内完成隔离静态目录与 Nginx location 基础配置。用户已授权补齐环境后继续部署，并指定 Shahrul Ikmal 单次内部 unlisted 视频与一条评论，不含 public 测试或其他平台发布。所有支持操作仅通过 SSH，禁止腾讯云管理后台。HK 媒体验收已完成；新表部署实况见 [部署记录](deployment-status-20260827.md)，不能以准备或离线测试代替最终外部发布成功。

## 随机模板与兼容

请求使用 `advanced_options.random_template={mode,source,layers}`。`source` 必填且仅为 `concat_video` 或 `no_bgm_video`；即使源普通输出未被选择，也允许内部生成但不对外返回。

- auto 从 FB v3 的 border 3、corners 3、opacity_video 5、tint 7 中稳定选择，共 315 种；light 不参与。manual 必须提供四层有效 asset ID。
- CPU 模板目录读取本地 `DRAMA_RANDOM_OVERLAY_MANIFEST_FILE` 指定的原始、SHA 固定的 FB manifest（7921 bytes），不请求 GPU catalog，也不复制/读取视频素材包。缺文件、指纹或内容无效时返回 503，不回退到 GPU。GPU 独立核验本机制作素材与冻结配方；其本地目录诊断、结果缓存/COS 可用性校验属于制作环节，不承担业务查询。详见 [CPU/GPU 职责与验收](cpu-gpu-boundary-20260827.md)。
- 冻结 source、asset manifest SHA、四层 asset identity、recipe/version、安全 rotation/scale/tint opacity 参数；重试复用同一 recipe。
- 结果字段固定为 `output_random_template_url` 与 `random_template_recipe`。
- recipe 审计 UI 仅以 DOM `textContent`/文本节点展示 version/profile/source/asset-set/layers；所有服务端值均视为不可信，禁止拼入 `innerHTML`。
- 新请求中的 `cover_template`、`naming_rule` 以及同名旧顶层字段只接受后忽略，归一为安全 `default`；历史值和展示保留。
- 上线前先备份 SQLite，dry-run 后以单事务、幂等方式把历史 `outputs_json` 补齐四个明确布尔；随机模板缺失为 false，不用新默认值重解释历史任务。

## 任务结果操作与短链

- 单一结果直接操作；多个结果先选素材。复制支持视频和封面；生成短链和 YouTube 只支持视频。
- 复制优先 Clipboard API，失败时用隐藏 textarea + `execCommand('copy')`。
- 每个 `(job_id, material_kind)` 只有一条不可变短链；content_id、目标和 wrapper SHA 同步冻结。
- 短链为 `https://gy.g2flow.com/s2l/youtube/<numeric>.html`；数字 ID 是短链表自增 ID，不等于 job ID。
- `gy.g2flow.com` 复用当前 X 渠道已经投产的域名、TLS 证书和 Nginx server；本需求不创建或修改 DNS/证书，也不改写现有 `/s2l/<X日志ID>.html`。只在现有 TLS server 中增加优先级更高的 `/s2l/youtube/` 精确静态 location。
- 目标固定为 `https://www.dramawavew2a.com/ads/101/2284/view`，参数顺序固定：`af_dp=<content_id>&c=ai_youtube&af_channel=ai_youtube&af_c_id=<job_id>`，严格 URL 编码。
- wrapper 无 open redirect；入口查询只能安全透传一个非空、有界 `fbclid`，任何核心参数或其他参数都不能覆盖/透传。文件原子不可覆盖；同内容幂等，不同内容冲突。

## YouTube 发布

- API 固定为 catalog、short-links、channels、youtube-publishes create/get/retry-comment 六个，详见 `api-doc.md`。
- 频道来自当前 app，`channel_status=1`，具有 refresh token、upload scope 与身份读取 scope；每次刷新 token 后、任何 mutation 前用 `channels?part=id&mine=true` 验证唯一实际 channel ID。未知/空/多条/不匹配关闭。
- 单选视频和频道；标题必填且不超过 100 字符；描述必填且 UTF-8 不超过 5000 bytes；评论可选。`{{url}}` 只在描述模板中 replace-all；出现宏时先确保当前素材短链，失败则不得排队或发起 YouTube 请求。模板与渲染值均冻结。
- 正式请求 privacy 固定 public；内部测试频道只用于另行授权的 unlisted canary，不出现在正式列表。固定 public 与 YouTube minimum functionality 的隐私选择要求存在合规风险，正式启用前须业务接受或改为三态。
- 主状态：`queued -> validating -> downloading -> uploading -> submitted -> processing -> published`，另有 `failed`、`unknown`。video ID 仅为 submitted；必须轮询处理完成和 visibility 后才 published。
- `comment_status` 与 `sync_status` 独立。评论只在 confirmed published 后执行；评论失败只重试评论。同步使用 outbox，失败不改变视频已发布事实。
- interrupted/5xx 查询 resumable session；无法证明未提交时 unknown 并禁止替代发布。processing/unknown 不重传。prior success 需二次确认。
- 评论非空须 `youtube.force-ssl`；关闭评论、儿童内容或权限不足只影响评论子状态。凭据仅服务端，日志禁止 secret/session URI。
- SQLite 表固定 `drama_youtube_publish` 和 `drama_youtube_sync_outbox`。使用现有 ads_aius 账号，但应用适配器仅允许 `ads_ai` 新 `ads_youtube_videos`、`ads_youtube_comments`、`ads_youtube_publish_log` 的 SELECT/INSERT/UPDATE，并发 1；原 MySQL 库只读、不双写。video/publish_log 完整冻结发布 payload，comment 完整冻结评论 payload，保留 payload_json/SHA256/canary marker，不截断、不构造旧队列 ID。外部 ID/发布 ID 唯一、同内容幂等、异内容冲突拒绝。CPU `127.0.0.1:18837` 受控 RPC health 必须为 v3/ads_ai，明确 shared-existing-account / application-table-allowlist / db_least_privilege=false；只检查所需能力，不宣称完整 grants 审计。每次写前确认结构/索引、TRIGGER 可见性、无 trigger/FK；18836 保留 FB。保留 root 客户端与 drama-youtube 服务端同值 0600 RPC token，writer DB JSON 仅服务端所有。三表已建成，本轮不再 DDL、不创建/修改数据库账号。坏配置、坏 payload 和必要能力/结构漂移 fail closed，outbox 保留 lease fencing。完整合同见 [ads_ai 新表发布合同](ads-ai-new-tables-20260827.md)。

- 原频道授权 JSON 经 CPU MySQL batch 查询时必须以 HEX 两列传输并严格解码一次；不得直接接受被重复转义的 scope，亦不得用字符串替换放宽授权。仅在 CPU 内存刷新 access token，不更新原 token/credentials 表。列表操作列直接展示三个新入口，详情入口继续保留。

## 香港 GPU 与验收

- release `/data/drama-synthesis-gpu/releases/<git_sha>`，`current` 原子 symlink；unit `drama-synthesis-gpu-worker.service` 仅监听 `127.0.0.1:8787`。
- 香港反向隧道把 CPU `127.0.0.1:18788` 映射到 HK 8787；legacy 18787 保留。
- GPU worker 只暴露制作相关 health/本地 catalog 诊断/render/cover 接口，校验完整 manifest/每文件 SHA，发布 COS 结果；CPU 页面与任务查询不调用其 catalog。GPU 不启动 `app.main()`、业务查询或任务恢复，不持有数据库/YouTube 凭据。
- 离线自动化验收覆盖历史兼容、315 recipe、冻结、精确短链/fbclid/原子冲突、宏、OAuth identity、resumable/processing、comment、outbox、unknown、防重、lease fencing、迁移并发和 GPU topology；这些用例的外部调用使用 fake/temp，不替代真实验收。真实 HK 小样与三表隔离恢复另行取证；指定频道的单次内部 unlisted/评论/三表回读须在合法数据库授权及生产部署门禁通过后执行，不得把代码 QA 当成真实发布成功。
