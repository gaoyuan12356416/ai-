# 固定视频资产清单

| 资产 | 部署路径 | 字节数 | SHA-256 |
| --- | --- | ---: | --- |
| 用户确认的新版片尾 `20260729-113038.mp4` | `/data/tt-post-publisher/assets/TT-new-outro.mp4` | 4,202,613 | `b6efd06c9304380aa118c4c3963057cc82e10ab569caa97d0cd9aeef588fe1fc` |
| DramaWave 圆角 Logo | `/data/tt-post-publisher/assets/dramawave-logo-rounded-132.png` | 10,147 | `3a159c7ec57d5ce526cb2bb406ddf364937495dd3e2f97dba0697c4339d6ad75` |

片尾媒体参数：

- 720 × 1280，30 fps，H.264 + AAC。
- 时长 11.766667 秒。
- GPU 默认成片保持原生 720 × 1280、30 fps、HEVC/H.265 + AAC，不再放大到 1080 × 1920；固定片尾源文件本身仍为上行所述 H.264 + AAC。
- 默认成片 profile 固定为 `tt-post-hevc-720x1280-v2`：HEVC 受控 VBR `900k/maxrate 1350k/bufsize 1800k`，AAC `128k`。60 秒样片 VMAF 89.79，已在当前后台链路与 Chrome 151 完整播放；TikTok 官方媒体规格支持 H.265。兼容回退 profile 为 `tt-post-h264-720x1280-v2`：H.264 `1500k/maxrate 2200k/bufsize 3000k`、AAC `128k`，60 秒样片 VMAF 90.24。4 GiB 仅为硬安全上限；34.8 分钟默认 HEVC 方案预计约 295 MB，H.264 回退方案预计约 433 MB，交付均必须低于 500 MB，完整生产成片待重跑。

部署必须先比对 SHA-256；不匹配时 worker fail-close，不生成或发布成片。已有 ready job 每次复用也必须重新哈希当前 Logo 与固定片尾，任一变化以 `prepare_idempotency_conflict` 拒绝旧成片。
