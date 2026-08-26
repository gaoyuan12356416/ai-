# 剧集合成：香港 GPU、随机模板、短链与 YouTube 发布

## 目标与边界

- CPU `43.166.187.96` 保留页面、任务队列、SQLite、OAuth、YouTube 发布和统一表同步；全部视频制作迁到香港 GPU `43.154.250.89`。
- 保持现有视觉、侧栏、表单、卡片和表格。输出键精确为 `concat_video`、`no_bgm_video`、`cover_16x9`、`random_template_video`；所有复选框新建时默认不勾选，服务端拒绝零输出。
- 不改香港 GPU 现有 `ads_video_producer.service`；不静默双写或自动回退旧 GPU。
- 本候选不执行部署、短链外写、真实 YouTube 上传/评论。生产代码部署授权也不包含任何真实 YouTube 上传/评论；外部发布必须另行精确授权。

## 随机模板与兼容

请求使用 `advanced_options.random_template={mode,source,layers}`。`source` 必填且仅为 `concat_video` 或 `no_bgm_video`；即使源普通输出未被选择，也允许内部生成但不对外返回。

- auto 从 FB v3 的 border 3、corners 3、opacity_video 5、tint 7 中稳定选择，共 315 种；light 不参与。manual 必须提供四层有效 asset ID。
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
- SQLite 表固定 `drama_youtube_publish` 和 `drama_youtube_sync_outbox`。统一适配器只允许 `ads_youtube_videos`、`ads_youtube_comments`、`ads_youtube_publish_log` 必要 SELECT/INSERT/UPDATE，并发 1、外部 ID 幂等；禁止 DELETE、DDL、任意 SQL。实体 payload 必须是 exact keys/types：video=`publish_id:int>0,video_id:str`，comment 再要求 `comment_id:str`，publish_log 与 video 同字段；禁止 extra/missing/错类型，external ID 必须分别等于 `video_id`、`comment_id`、十进制 `str(publish_id)`。worker 必须从受控 RPC factory 构造 writer：仅 HTTPS 或 loopback HTTP、禁止 redirect、固定 timeout，凭据只从 server-only 0600 文件读取。缺配置/认证/表/响应均 fail closed；claimed outbox 的坏 JSON、非 object、合同错误也必须按原 lease fencing 标记 failed，不得记录原 payload。

## 香港 GPU 与验收

- release `/data/drama-synthesis-gpu/releases/<git_sha>`，`current` 原子 symlink；unit `drama-synthesis-gpu-worker.service` 仅监听 `127.0.0.1:8787`。
- 香港反向隧道把 CPU `127.0.0.1:18788` 映射到 HK 8787；legacy 18787 保留。
- GPU worker 只暴露 health/catalog/render，校验完整 manifest/每文件 SHA，发布 COS 结果；不持有 YouTube 凭据。
- 验收覆盖历史兼容、315 recipe、冻结、精确短链/fbclid/原子冲突、宏、OAuth identity、resumable/processing、comment、outbox、unknown、防重、lease fencing、迁移并发和 GPU topology；全部外部调用使用 fake/temp。
