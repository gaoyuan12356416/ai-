# 测试用例

| 用例 | 验证点 | 结果证据 |
| --- | --- | --- |
| C1 | 缓存损坏、同大小且恢复 mtime、缺回执、错误来源、无 timing_basis | 自动测试拒绝 |
| C2 | 25→30fps 舍入、21ms 音画起点差、提前 EOF | 自动测试及 FFmpeg 帧索引对照 |
| C3 | H.264 SPS 1280×720→720×1280、关键帧重建 | 自动测试 + 130 秒 3250 源帧 PTS/尺寸一致 |
| C4 | 120 秒寻址、30fps 横屏 | 250 / 300 帧 PTS 一致，抽样 NV12 零差异 |
| C5 | 所有 18 项 RGBA 像素与原始时长 | 缓存构建器逐帧 MD5 + PTS/duration |
| C6 | 非整数相位透明动画循环 | 两个选中动画各 90 帧哈希匹配 FFmpeg 完整循环 |
| C7 | Python 子进程进度、超时、终止证据 | 真实短子进程 + 现有 runtime/媒体回归 |
| C8 | 同输入画面/性能 | 固定 30 秒 SSIM、抽帧和 CPU/wall 测量 |
| C9 | 120+10 秒分片、AAC、最终文件、重复读取检查点 | GPU 整链路与续跑 |
| C10 | 精确 release、服务用户、原任务恢复 | 启动预检与生产证据 |

命令：`python -m unittest scripts.test_drama_gpu_frame_pipeline scripts.test_drama_gpu_compositor_v2 scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_throughput scripts.test_drama_synthesis_remote_client scripts.test_drama_synthesis_media_pipeline`。
