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
