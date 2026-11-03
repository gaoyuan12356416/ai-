# 验证进展

状态：协调器、X 原生服务和 CPU 节点运行 668d0b2，GPU 已升级 CA 修复版 b349295 并于 23:25:10 激活；四渠道保持启用。新流程已取得 Facebook 和 X 最终平台发布回执。TT 两条任务保持 unknown，GPU TLS 信任链修复及实际进程环境验证已完成，没有重试原任务。23:25:59 两节点共 9 个新鲜心跳 worker、active_jobs=0、待 ACK 回执=0。当前没有合格待接 TT 新任务，TT/YouTube 真实新流程业务验收仍待正常任务，本文不是四渠道全部业务验收完成声明。以下时间均为 2026-11-02 CST，带 Z 的原始证据采用 UTC。

## 自动化验证

`python -m unittest discover -s scripts -p 'test_pipeline*.py'`：347 项通过，54.480 秒（23:05:29，668d0b2，主代理运行记录）。覆盖原子竞争、跨阶段账号/Page/频道互斥、单条轮转、未知回执锁、迟到回执、冻结配方和成片复用、审核/明确重试、历史暂停、权限、动态节点及容量调节。阶段统计 UI 契约检查通过；TT worker 相关 91 项通过，3.714 秒，X 独立复验 47 项通过。此前 ba2ee7f 的 TT backend/bridge/recovery/source-lock 独立 37 项通过，7.938 秒；新领取按实际 backend 冻结，CPU 自动随机 H264/HEVC 显式映射软件 profile，既有描述器续代不改。

较早验证包括执行器与既有 GPU renderer 扩展 174 项、旧 TT 及渠道账本 303 项、TT 旧回执只读核对相关 193 项；这些是对应阶段的结果，不合并计为最终版本的测试数量。Python 3.9 语法兼容、独立原生运行目录导入和补丁演练通过；导入预检禁止数据库连接、网络、子进程和文件写入。

后续 b349295 仅修安装器/doctor 的可移植 CA 配置与空信任链检查，相关 41 项及独立 45 项通过。347 项完整回归对应 668d0b2，不将增量回归合并为新的完整套件数量。

Chrome 本地桌面/移动、普通用户/管理员、三节点显示、自然排空、深链和既有 SocialKit 导航通过。线上浏览器操作被计算机工具中止后未恢复，线上只确认 HTTP、静态资源及权限边界，不宣称线上浏览器完整验收通过。

既有 X 回归 `test_x_post_catchup_sidecar.test_drama_pool_readonly_http_hold_keeps_precise_business_error` 在本分支和原始 6700188 均失败（200 空列表与预期 409 的既有契约差异）；未创建发布任务，相关函数未更改。

## 新流程真实业务

21:39:35 快照确认：CPU 制作 3 条，GPU 制作 1 条；CPU/GPU 各有 1 条 Facebook 最终平台发布回执。只计 generation>0 的新节点执行，编码探针、COS 探针和旧执行器结果不计入。

| 节点 | 阶段 | 中央任务 / 原账本来源 | 已确认结果 |
| --- | --- | --- | --- |
| cpu-main | 制作 | 3136 / fb:auto:29876 | completed，160,545,471 bytes |
| cpu-main | 制作 | 3138 / fb:auto:29884 | completed，107,934,365 bytes |
| cpu-main | 制作 | 3141 / fb:auto:29905 | completed，472,838,347 bytes |
| hk-gpu | 制作 | 3140 / fb:auto:29895 | completed，1,009,219,915 bytes |
| cpu-main | 发布 | 1753 / fb:video:5b2234fea209480fa1b1d1e8a4737c98 | published，平台 ID 4579245452332068 |
| hk-gpu | 发布 | 1754 / fb:video:bfbefc6b0b354cdf90558d16bc1a0836 | published，平台 ID 1662933568743523 |

CPU/GPU 实际 FFmpeg 分别在 13:01:19.540Z、13:01:26.980Z 开始，CPU Graph 提交在 13:01:21–13:01:38Z 发生，证明制作与发布实际重叠。21:13:15 停止新领取后，剩余制作自然排空；CPU 3141 于 21:37:09 完成。21:38:51 中央 executing=0，CPU 未 ACK 回执=0；21:39:00 GPU 未 ACK 回执=0。该排空过程没有中断节点或清除任务租约。

CPU 3138 成片 320.436 秒，render 414.415 秒；CPU 3136 成片 576.533 秒，render 714.717 秒。GPU 3140 成片 1621.708 秒，render 710.753 秒、COS 上传 20.667 秒。CPU 3141 成片 1341.087 秒，render 1405.398 秒、COS 上传 202.502 秒。内容与 profile 不同，这些是业务样本，不能作为同内容 CPU/GPU 对照基准。

双向跨机成片只读验证均 HEAD/GET 200，完整 SHA256 与大小匹配，下载临时文件已清理：CPU 3138→GPU，107,934,365 bytes，8.223 秒；GPU 3140→CPU，1,009,219,915 bytes，851.020 秒，约 1.19 MB/s。后者是本次明确的跨机下载瓶颈样本，不代表固定网络带宽，也不是重新上传平台。

## TT 故障边界与修复验收限制

TT 4538 在素材资格校验阶段失败；共享 selector 的旧接口兼容已补回归，但没有自动重试该失败任务。TT 4530、4533、4528、4537 在 creator_info 检查时失败；21:53:04 原账本审计中 `task_publish_started_count=0`、`pipeline_tt_operation=0`，未发生平台发布 POST，不自动重试。

TT 4540/4541 曾经绕过桥接的 MediaPort 进入原 GPU 制作。两条原制作结果和 manifest 完整保留，任务最终为 failed，不能计为新节点制作成功，也不自动重做。历史制作使用的 font SHA 缺乏当时冻结证据，不能用当前字体哈希倒推确认原配方。

路由修复覆盖生产 frozen route 的客户端拦截；source_key 锁允许不同来源并行而保持同源跨阶段互斥。首次 direct_outro 制作需比对 CPU 权威批准的片尾/字体 SHA，CPU/GPU profile 与资源能力按实际路由冻结；既有描述器续代不换配方。TT 原生 ba2ee7f 仅更新 tt_native.py，保留现网 publisher；本次临时旧 GPU 连接规则已全部精确移除。

GPU 旧版 1a83de8 的 doctor 曾返回成功，但随后检查发现 direct_outro 小样为 25fps，未满足 30fps 契约；旧检查未检验实际帧率，不能作为完整规格通过证据。fb4704d 在真实片尾输出命令中固定 30fps，预检同时检查输出的两项帧率、视频规格及 AAC 音频。CPU 原 FFmpeg 缺少 drawtext 的问题通过独立、root-owned 的 TT binary 解决，保留原服务 binary。

两节点 668d0b2 的安装器 verify 均完成实际 X repair 命令和严格 TT direct_outro 30fps/AAC 检查。CPU 5 个 TT profile、GPU 9 个 TT profile 均完成本机编码预检；CPU 于 15:08:13Z 激活（PID 963393），GPU 于 15:09:00Z 激活（PID 3952199）。编码/COS 探针不计业务完成，也未覆盖随后发现的官方 API TLS 证书问题。

TT 4534/4535 在 GPU 各执行一次 init 后进入 unknown，错误为 tt_upstream_unavailable，没有 publish_id 或成功 checkpoint，未重试。只读检查确认 GPU urllib 的默认 CA 数量为 0，OpenSSL 默认位置指向服务用户不可读的旧 root/miniconda 路径；使用 certifi 的子进程覆盖后 CA 数量为 120，未认证的官方 HEAD 请求获得 404，证明该覆盖可建立 TLS，不证明任务已经发布或可以重发。

GPU 单独优雅停接后完成 CA 修复 b349295，于 15:25:10Z 重新激活（PID 3960674），保留原节点 identity。15:25:22Z 从实际进程环境复验：默认 CA=120，X/TT 的 urllib 官方 HEAD 均返回 404，TLS 验证保持开启。CPU 668d0b2/PID 963393 未重启，两机运行 checkout 均 clean。两条 TT unknown 保留原锁，未因修复重试 init；当前没有合格待接 TT 新任务，不能用这次无认证 TLS 检查替代发布验收。

## X 自然批次与故障修复

22:37 的 X 批次 416 正常选材并创建 canonical queue；无候选任务以 no_candidate 结束，没有重发。CPU 已完成 5380/5381 的源媒体预检和成片传输，5380 随后由 GPU 领取发布；GPU 预检完成的 5386 随后由 CPU 领取发布，证明双向跨机交接实际发生。这些 source profile 预检没有重新编码，不能追加到上文 CPU/GPU 实际制作样本，也不能把下载成功记为平台发布成功。

5380 在 media initialize 处失败，原账本为 x_upstream_error，没有 media ID/Post ID，也未执行发帖请求。原平台 HTTP 状态与响应正文当时未保存，不能追溯判定是平台授权、限流、媒体规格或网络问题。CPU 原生与 GPU 节点的 XApiClient/HTTP 实现及生产源码摘要相同；现有证据没有证明短期凭据解析错误，也不足以替平台原响应补结论。

5381 的账号 19 在 14:43:24–14:47:41Z 发生 74 次成功身份核验，14:47:45Z 身份核验以 x_post_rate_limited 失败。旧桥接在每个媒体分片和状态查询前都重复请求身份接口，偏离原 publishing_token_provider 的 refresh-only 语义。该任务虽未走到发帖 POST，原 unknown 状态与账号锁仍保留；当前两条 X unknown 均不自动重发或清锁。

668d0b2 保留新领取时的强身份核验，以及每次操作的原账号审批、App、scope、Premium 和身份检查；常规操作仅按原规则刷新令牌，不逐分片重复查询身份。仅媒体请求收到 401 时允许一次受控 verify_now/重试，绑定原任务、账号、generation、操作及完整路径/query/body 指纹；同一 claim 最多一次，initialize 重试不增加发布 attempt。连续 401、发帖/转发 POST 401 不重试，不重跑整任务。真实 OAuth/native SQLite fixture 覆盖重复 append/status 不重复身份查询、过期刷新、门禁变化、重试边界及 unknown 保留。

5382/5383/5384 的 CPU 预检曾以 media_transcode_failed 结束，原因是软件 libx264 路径残留 NVENC 专用 tune=hq。cf894ea 去除该 CPU 参数，节点预检改为执行实际 X repair 完整命令；两机新版完整验证已通过，历史失败任务不自动重试。

修复后的自然任务 X 5385、5387 均由 CPU 发布成功：5385 于 15:19:36Z 获得 Post ID 2117275437915160647，5387 于 15:20:05Z 获得 Post ID 2117275561353519477。两条均为 generation1/attempt1，完整 checkpoints、原生账本与中央发布事实一致。这两条是新流程的真实 X 最终平台回执，未重试前述失败或 unknown 任务。

新失败回执持久化脱敏错误与可核实的 HTTP 状态；只有安全消息中的明确 HTTP 标记才能形成上游状态字段，不将 sidecar 502 或节点任意字段当平台状态。诊断导入、字符串化或脱敏异常时舍弃诊断字段，继续持久化原失败 code/unknown/阶段信息，不能因诊断失败丢回执。明确的代理限流按当前冻结资源记录 cooldown，中央再次验证资源归属；此逻辑不清 unknown、不授权重发。

## 暂停、防重与监控

最终切换核对的原生 hold 数仍为 X queue 88、X auto 679（合计 767）、FB 31、TT auto 653、YouTube 3；四渠道恢复接单没有恢复这些旧积压。原素材预留、去重与发布回执保留。7 条 X waiting_relay 经核对无 publish_log/repost/claim/operation/checkpoint 后才严格暂停，未重发源帖或重置队列。未来 YouTube 预约保留原 video_id/预约时间，接续只允许查询及已授权首评，不能再次上传；同频道历史 unknown 锁持续保留。

已完成的 YouTube 上传父记录仅在 exact child 回指、原父租约失效时收敛；真实活动子上传和未知锁不豁免。FB 原提前制作、TT 原提前 4 小时资格保留，发布不提前，历史暂停仍按原发布时间。已发布来源的旧 gen0 制作观测只记 metadata_only，不虚增制作吞吐。

23:25:46 最终监控 summary/workers/tasks 授权读取均 200，样本耗时 73/7/18ms；唯一原生发布事实总数、全部 8 类状态与中央投影一致，动态节点与实际注册/配置一致。两名真实权限用户的内部可信 actor 只读检查无越权（23:11:17 样本分别为 81/0 行）；普通用户范围为 mine、隐藏全局 workers，未伪造外部登录。未登录的三个公开 API 均 401，根页面及监控 HTML/JS/quick-nav 均 200。根页面只在无业务 hash 时转监控首页，业务深链和 SocialKit 保留。

新增服务器使用一次性登记与实测能力，不需要新增代码白名单。第三节点的协议/UI、100 字符登记名、每阶段容量配置及自然排空均有离线覆盖；实际只部署 CPU/GPU 两台，不宣称第三台已上线。容量检查仍每 60 秒进行，至少 3 个近 6 小时合格完成样本、有可领任务且有资源余量才考虑加 1，5 分钟内最多增加一次；持续高 CPU 两分钟或下一次评估发现可用内存低于 15% 时减小目标，不中止在途任务。

证据存于本次私有审计目录：`pipeline-business-drained-final.json`、`pipeline-business-acceptance-handoff.md`、双向 `pipeline-*-artifact-read-on-*.json`、`pipeline-tt-four-publish-no-write-events.json`、`pipeline-tt-preserved-manifest-binding.json`、`pipeline-x416-identity-query-audit.json`、`pipeline-x416-failures-latest.json`、`pipeline-x-client-*-fingerprints.json`、`pipeline-hk-gpu-b349-live-tls.json`、`pipeline-hk-gpu-b349-final-audit.json`、`pipeline-cpu-main-post-ca-final-audit.json`、`pipeline-monitor-acceptance.json`。不将认证信息、原始环境或带签名 URL 入库。

## 最终运行状态与验收边界

23:25:59 四渠道保持启用，旧触发器按原状态恢复，本次临时端口/防火墙规则已全部精确清理。中央 9 个 worker 心跳新鲜：CPU 制作 4/发布 2，GPU 制作 1/发布 2；active_jobs=0，CPU outbox 待 ACK=0/已 ACK=28，GPU 待 ACK=0/已 ACK=8。空闲原因按真实账号锁或无可领任务显示，不代表 worker 离线。原 holds、未知锁及预约仍保留。

X 5385/5387 最终发布成功；TT 4534/4535 为 unknown 并保留锁，所有历史失败/unknown 未重试。当前没有合格待接 TT 新任务，TT 和 YouTube 尚无本次新节点最终发布成功证据，需等正常业务任务验收；不额外创建测试帖子或恢复历史任务填补矩阵。

## 2026-11-03 运维交接探针限项修正

TT renderer 使用 `mkdtemp(prefix=job_id + '.')` 创建临时工作目录。只检查 `jobs/<job_id>` 不能证明不存在在途文件；只读交接探针现在同时拒绝精确 `jobs/<job_id>.*` 目录，覆盖默认及冻结 profile 目录。真实临时目录 fixture 证明后缀目录会阻止交接，其他 job 前缀不会误匹配。此修正仅涉及运维工具，不部署节点、不重启服务、不重复执行交接；已审交接另保留原生 attempt=0、原始 claim/冻结请求、无 descriptor delivery/操作/回执等独立证据，不以这个单项文件探针作为未执行证明。
