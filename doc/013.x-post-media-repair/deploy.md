# 013.x-post-media-repair 部署文档

## 拓扑

```text
CPU daily runner
  -> 127.0.0.1:18820
  -> SSH reverse tunnel
  -> GPU 127.0.0.1:8820 worker
  -> NVENC
  -> immutable COS object
  -> CPU download + SHA/size/probe
  -> frozen queue
```

## 部署顺序

1. 本地全量回归、语法检查和差异检查通过后提交并推送 GitHub。
2. CPU 备份 SQLite、daily env、systemd unit 和当前 release 指针。
3. GPU 备份共享 COS env，收紧为 0600；从精确 GitHub commit 构建独立 release。
4. 在 CPU 生成独立修复 Bearer，不输出内容；GPU 经现有 SSH 信任链安全拉取同一文件。
5. GPU 安装 worker unit 和 tunnel unit，先验证 GPU 本机 health，再验证 CPU 回环 health 和错误 Bearer 403。
6. CPU 从同一精确 commit 构建 release，安装 daily unit，增量加入 repair 配置，切换 symlink 并只重启 X sidecar。
7. 验证 SQLite 新列、计划只读恢复接口、timer 和既有发布记录。
8. 使用显式素材 ID 运行只修复/复检 backfill；先单条 canary，再处理剩余目标。
9. 核对 pool 校验状态、COS/manifest、queue/log/Post 零新增以及服务最终状态。

## 回滚

- 停止并禁用新 worker/tunnel，恢复 GPU 旧配置权限或备份。
- CPU 原子切回旧 release，恢复旧 daily unit/env 后 daemon-reload 并重启 X sidecar。
- SQLite 新列为向后兼容增量字段，旧代码可忽略；不得因回滚删除真实发布日志。
- 已生成的 content-addressed COS 对象和 manifest 保留，避免破坏后续审计与已冻结队列。
- backfill 只清除已通过复检的 pool 错误；如需恢复旧展示，可依据部署前 SQLite 备份逐条核对，不做整库盲目覆盖。

## 2026-07-24 生产部署记录

- GitHub/CPU/GPU 运行版本：`1f607dff4e4fde1c11931f32ab1d477adf5b610f`。
- CPU release：`/opt/x-post-automation/releases/1f607dff4e4fde1c11931f32ab1d477adf5b610f`。
- GPU release：`/opt/x-post-media-repair/releases/1f607dff4e4fde1c11931f32ab1d477adf5b610f`。
- CPU 部署前备份：`/mnt/data-disk/x-post-automation/backups/20260724T162929+0800-gpu-media-repair`。
- GPU 部署前备份：`/data/x-post-media-repair-backups/20260724T162929+0800-predeploy`。
- 回填报告目录：`/mnt/data-disk/x-post-automation/backfills/20260724T163600+0800-gpu-media-repair`。
- CPU 旧 release：`/opt/x-post-automation/releases/622a8caff321dc297871d7cea354ad8d5fed4e52`。
- 当前九条采用 warm-cache：素材池不覆盖原始 `custom_source.url`；GPU manifest 保留 content-addressed COS 成品。每日任务选中后读取同一成品，CPU 重新下载并通过正式 probe 后，才把修复 URL 冻结到队列。
- 回填工具只调用素材查询、校验写回和 GPU 修复，不调用 daily plan 或 publish；上线当日发布数量未变化。

## 2026-07-28 短剧资源 ID 兼容修复与真实发布

- 根因分为两层：短剧第 1 集源地址来自 `img.tianmai.cn`，原片为可下载的 HTTPS
  视频但尺寸不合规；随后 GPU repair 仍把 `ads_drama_resource.id` 的 32 位小写
  十六进制资源 ID 当作正整数校验，导致转码请求在 GPU/COS 前被拒。
- 当前开发线修复提交为
  `c604bd56a44978055fc4972babe8f742829b0d78`；GPU 为避免带入基线之后无关的
  X publisher 变更，从原 GPU 生产提交回补并部署精确 GitHub 提交
  `362e69766dc6ba828a9d9b8940a75ac4e11ec69d`。
- GPU release：
  `/opt/x-post-media-repair/releases/362e69766dc6ba828a9d9b8940a75ac4e11ec69d`；
  release 内 repair/daily/backfill 70 项回归通过。数字素材 ID 路径保持
  `material-<id>`，短剧资源使用
  `drama-resource-<32位小写十六进制ID>`；`pool_item_id` 仍只接受正整数。
- CPU 配置备份：
  `/mnt/data-disk/x-post-automation/backups/20260728T032510Z-drama-media-host`。
  GPU 配置备份：
  `/data/x-post-media-repair/backups/20260728T032525Z-source-host`。
  GPU 代码部署备份：
  `/data/x-post-media-repair/backups/20260728T033520Z-resource-id-1f607df`。
- 仅在 schedule、sidecar 和 GPU repair 三个精确白名单中追加
  `img.tianmai.cn`，原 COS 域名保留；sidecar 与 GPU worker 窄重启后 health
  正常，schedule oneshot 继续按启动时读取配置。
- 第 1 集原文件 SHA-256 为
  `3fdd687d80c9a5ee8457515b0eb61d17f80b6730bbfc0a164d66e2f48ef5336e`，
  45,362,375 字节。NVENC/COS 修复件 SHA-256 为
  `ceac94935080cb82b4d2520272fad389c86b2bc6c362c00081a1b90c7ed46645`，
  60,037,206 字节；CPU 二次下载验证为 H264/yuv420p、AAC、720x1280、
  30fps、103.766667 秒。
- 原批次 `4` 在修复前为 `failed_preflight`，无 queue、log 或未知结果。
  旧短剧池记录 `1` 按现有 delete/add 契约重新校验为记录 `2`，随后复用原
  批次且只冻结一条 Episode 1 队列。
- 真实发布结果：run `4` completed，queue `35` published，log `35`
  published，X Post
  `https://x.com/SecretAffa6ann/status/2081948564918333677`，短链
  `https://ai.yingliangads.com/s2l/35.html`。短剧池进度为已发布 1/11、下一集
  2；全库无 `post_creating` 或 `unknown_outcome`。
- 短链页面、跟踪参数键、`AIpost` 渠道、queue/log/content/material 绑定均已
  核对，SQLite `integrity_check=ok`；主 API、sidecar、claim timer、worker
  timer、GPU worker/tunnel 均 active，旧 daily timer 保持 masked。
- 回滚代码时只切回 GPU 旧 release
  `/opt/x-post-media-repair/releases/1f607dff4e4fde1c11931f32ab1d477adf5b610f`
  并使用上述配置备份；本次已经产生真实 Post、queue、log、短链和剧集进度，
  禁止用部署前 SQLite 覆盖当前审计事实。

## 2026-07-29 超长剧集裁尾部署方案

1. 以 GitHub 精确提交同时构建 CPU `/opt/x-post-automation/releases/<commit>`
   与 GPU `/opt/x-post-media-repair/releases/<commit>`。
2. 备份 CPU 在线 SQLite、Token 非秘密哈希/权限、daily env、release 指针及
   受影响源码；备份 GPU worker env/unit、manifest 统计和 release 指针。
3. 暂停 `x-post-schedule.timer` 与 `x-post-schedule-claim.timer`，等待正在运行的
   oneshot 结束；不得启动或恢复 10:06 失败批次。
4. 先切 GPU v2 worker，再把 CPU
   `X_POST_DAILY_REPAIR_PROFILE` 改为
   `x-h264-nvenc-720-trim139-v2` 并切 CPU release；在 timer 暂停期间完成。
5. 核对 GPU 本机 `127.0.0.1:8820/health`、CPU 隧道
   `127.0.0.1:18820/health` 和 CPU 实际配置 profile 三者一致。
6. 在共享调度锁下运行 `scripts/x_post_drama_media_repair_backfill.py`，精确指定
   池 53/54、各自 content ID、Episode 1 和旧错误
   `source_not_repairable`。命令先 dry guard，再真实 GPU/COS/CPU 复验，全部
   成功后才一次事务恢复 `pending`；报告写入 data disk。
7. 只读核对原 URL、池 ID/FIFO/进度、账号粘性、10:06 旧 run/queue/log、
   SQLite integrity 和重复组均未改变；恢复 timers。
8. 不手工启动 schedule oneshot。由下一个自然发布点完成全账号原子建队列与
   顺序发布，并核对 queue/log/Post。

回滚：在自然发布前重新暂停 timers，CPU/GPU 分别切回部署前 release，并恢复
CPU profile v1；保留 v2 COS/manifest 与 SQLite 事实。若池 53/54 已通过复验
恢复但尚未发布，代码回滚后应暂停短剧 timer并人工评审，禁止直接用旧备份覆盖
SQLite。若已产生 Post，任何回滚都不得删除 queue/log/绑定或回退剧集进度。

## 2026-07-29 超长剧集裁尾生产记录

- 15:32 CST 确认 `x-post-schedule.timer`、
  `x-post-schedule-claim.timer` 及两个 oneshot 均为 inactive 后开始部署；
  部署和 backfill 期间未启动旧批次。
- GPU 使用提交
  `b6f95f3874a9bb187aa7e8c7faac6254893ba787`，release 为
  `/opt/x-post-media-repair/releases/b6f95f3874a9bb187aa7e8c7faac6254893ba787`，
  备份为
  `/data/x-post-media-repair/backups/20260729T153604+0800-prep-b6f95f3`。
  本机及 CPU 隧道 health 均返回
  `x-h264-nvenc-720-trim139-v2`。
- CPU 首次使用同一提交，release 为
  `/mnt/data-disk/x-post-automation/releases/b6f95f3874a9bb187aa7e8c7faac6254893ba787`，
  全量备份为
  `/mnt/data-disk/x-post-automation/backups/20260729T153641+0800-duration-trim-prep-b6f95f3`；
  SQLite 在线备份 `integrity_check=ok`。
- 首次生产 backfill 在 GPU 前按门禁失败，错误为
  `media_host_not_allowed`。原因是 standalone 恢复脚本只安全读取 daily/token，
  未读取正常 schedule unit 已使用的 `/etc/x-post-schedule.env`，因此遗漏
  `img.tianmai.cn`。失败报告保留在
  `/mnt/data-disk/x-post-automation/backfills/20260729T1547+0800-duration-trim/pool-53-54.json`；
  当时池 53/54、队列、日志和 GPU manifest 均未改变。
- 只在短剧恢复脚本增加严格三层配置解析
  `daily -> 独立 repair token -> schedule`；schedule 文件只允许现有非秘密键，
  明确拒绝 internal token、MySQL password 和 repair token，并将异常输出改为
  脱敏。修复提交为
  `7a20f05ecc760a79f3776fded08d47ccfa76d5d8`，CPU release 为
  `/mnt/data-disk/x-post-automation/releases/7a20f05ecc760a79f3776fded08d47ccfa76d5d8`；
  第二恢复点为
  `/mnt/data-disk/x-post-automation/backups/20260729T1557+0800-schedule-loader-predeploy-7a20f05`。
  GPU 业务代码未变化，继续使用 `b6f95f3` release。
- 成功报告为
  `/mnt/data-disk/x-post-automation/backfills/20260729T1602+0800-duration-trim-7a20f05/pool-53-54.json`，
  SHA-256 为
  `e908d9d4eb50f1310d9e5189e15b767fcf622f452f6a00892d2cddfdee502471`。
  池 53 源时长 `182.791667s`、池 54 源时长 `171.52s`；两者均固定输出
  `139.0s`，H264/yuv420p、AAC、720x1280，GPU manifest v2 为 ready，
  CPU 二次下载 SHA/大小/probe 全部匹配。
- 两项全链成功后于 16:09:38 CST 一次恢复为 `pending`；池 ID、FIFO、
  Episode 1、未绑定状态和零队列历史保持不变。10:06 run `14` 仍为
  `failed_preflight`，queue/log 均为 0；账号 10 仍绑定池 2、下一集 Episode 8。
- 16:12 CST 只读候选预演为 6/6：账号 10 续发池 2 Episode 8；账号 9/8
  分别领取池 53/54 Episode 1；账号 7/6/5 领取池 57/60/131 Episode 1。
  SQLite `integrity_check=ok`，重复剧集组、`post_creating`、
  `unknown_outcome` 均为 0。
- 16:12 CST 恢复两个 timer；旧 daily timer 继续 masked。下一自然
  16:20 批次的 queue/log/Post 结果完成后追加，不手工启动或重放。
- 16:20 自然 run 17 完成全部媒体预检并原子建出 6 条队列；自然新增的
  pool 2/57/60/131 四份 v2 manifest 全部 ready，其中 pool 131
  `212.666667s -> 139.0s`，证明自然调度已实际执行超长裁尾。
- 首队列 45 在 X 前因 `invalid_short_base_url` 停止；attempt=0、
  unknown=0、X ID/URL 和短链/文案均为空，其余五条无 log。磁盘
  `/etc/x-post-automation.env` 在 14:44 已被改为代码不允许的
  `https://gy.g2flow.com/s2l`，15:46 sidecar 重启后才激活该漂移。
  16:40 再次暂停 timer；按 `BUG-002.md` 修复配置并执行严格零尝试恢复后，
  才允许 frozen run 继续，禁止新建计划或直接发布。真实恢复的报告必须是
  `/mnt/data-disk/x-post-automation/recoveries/` 下的全新 JSON 文件；路径
  越界、符号链接祖先、既有目标或缺少报告均失败关闭。
