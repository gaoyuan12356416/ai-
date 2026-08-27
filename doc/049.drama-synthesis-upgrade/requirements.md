# 剧集合成：香港 GPU、随机模板、短链与 YouTube 发布

## 目标与边界

- CPU `43.166.187.96` 保留页面、任务队列、SQLite、OAuth、YouTube 发布和统一表同步；全部视频制作迁到香港 GPU `43.154.250.89`。
- 保持现有视觉、侧栏、表单、卡片和表格。输出键精确为 `concat_video`、`no_bgm_video`、`cover_16x9`、`random_template_video`；所有复选框新建时默认不勾选，服务端拒绝零输出。
- 不改香港 GPU 现有 `ads_video_producer.service`；不静默双写或自动回退旧 GPU。
- 已在现有 `gy.g2flow.com` 站点内完成隔离静态目录与 Nginx location 基础配置；未生成真实 YouTube 短链、未执行生产统一表写入或真实 YouTube 上传/评论。2026-08-27 用户已明确授权补齐环境后继续部署，并指定 Shahrul Ikmal 测试；按附件仅允许单次内部 unlisted 视频与一条评论，不含 public 测试或其他平台发布。所有支持操作仅通过 SSH，禁止腾讯云管理后台。HK 隔离环境与媒体验收已完成，CPU 正式发布仍被目标库合法授权阻塞；精确状态见 [部署记录](deployment-status-20260827.md)。

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
- SQLite 表固定 `drama_youtube_publish` 和 `drama_youtube_sync_outbox`。统一适配器只允许 `kunlunads_dev` 现有 `ads_youtube_videos`、`ads_youtube_comments`、`ads_youtube_publish_log` 必要 SELECT/INSERT/UPDATE，并发 1、外部 ID 幂等；运行期禁止 DELETE、DDL、任意 SQL。video/publish_log payload 精确冻结 `publish_id,video_id,app_id,channel_local_id,operator_user_id,job_id,content_id,source_kind,source_url,title,description_rendered,privacy_status,published_at_utc`；comment 精确冻结 `publish_id,video_id,comment_id,channel_local_id,operator_user_id,comment_text,published_at_utc`。禁止 extra/missing/错类型，external ID 必须分别等于 `video_id`、`comment_id`、十进制 `str(publish_id)`。worker 只访问 `127.0.0.1:18837` 受控 RPC；18836 保留给现有 FB 随机模板隧道。RPC token 与数据库凭据必须成对配置；同一 32+ 字符 token 以两份同值文件分别交给 root 客户端和 `drama-youtube` 服务端，每份均由其当前进程账号所有、非 symlink、精确 0600；writer DB JSON 只归 `drama-youtube`。运行 health 必须读回精确三表 schema fingerprint、唯一索引、账号身份和三表级 SELECT/INSERT/UPDATE grants；任何 schema wildcard、额外表/权限或 GRANT OPTION 均 fail closed。一次性 migrator 与长期 writer 必须为两个账号，writer 永不获得 DDL。缺配置/认证/表/响应均 fail closed；claimed outbox 的坏 JSON、非 object、合同错误也必须按原 lease fencing 标记 failed，不得记录原 payload。

## 香港 GPU 与验收

- release `/data/drama-synthesis-gpu/releases/<git_sha>`，`current` 原子 symlink；unit `drama-synthesis-gpu-worker.service` 仅监听 `127.0.0.1:8787`。
- 香港反向隧道把 CPU `127.0.0.1:18788` 映射到 HK 8787；legacy 18787 保留。
- GPU worker 只暴露 health/catalog/render，校验完整 manifest/每文件 SHA，发布 COS 结果；不持有 YouTube 凭据。
- 离线自动化验收覆盖历史兼容、315 recipe、冻结、精确短链/fbclid/原子冲突、宏、OAuth identity、resumable/processing、comment、outbox、unknown、防重、lease fencing、迁移并发和 GPU topology；这些用例的外部调用使用 fake/temp，不替代真实验收。真实 HK 小样与三表隔离恢复另行取证；指定频道的单次内部 unlisted/评论/三表回读须在合法数据库授权及生产部署门禁通过后执行，不得把代码 QA 当成真实发布成功。
