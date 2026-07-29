# 固定视频资产清单

| 资产 | 部署路径 | 字节数 | SHA-256 |
| --- | --- | ---: | --- |
| 用户确认的新版片尾 `20260729-113038.mp4` | `/data/tt-post-publisher/assets/TT-new-outro.mp4` | 4,202,613 | `b6efd06c9304380aa118c4c3963057cc82e10ab569caa97d0cd9aeef588fe1fc` |
| DramaWave 圆角 Logo | `/data/tt-post-publisher/assets/dramawave-logo-rounded-132.png` | 10,147 | `3a159c7ec57d5ce526cb2bb406ddf364937495dd3e2f97dba0697c4339d6ad75` |

片尾媒体参数：

- 720 × 1280，30 fps，H.264 + AAC。
- 时长 11.766667 秒。
- GPU 加工时统一到 1080 × 1920、30 fps、48 kHz 双声道。

部署必须先比对 SHA-256；不匹配时 worker fail-close，不生成或发布成片。
