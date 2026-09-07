# TT / FB 随机模板 GPU 合成

将原 CPU 旋转、随机缩放、四个透明装饰层的缩放及叠加改为短剧同源 OpenCL 融合内核。源解码、源画面规格稳定和音频仍由 CPU 完成；成片继续用 NVENC 编码。

保留 TT HEVC Main / hvc1 / P6 / 720×1280 / 30fps / AAC 128k，以及 FB H.264 High / P5 / 720×1280 / 30fps / AAC 192k。保持 v3 profile、随机 recipe、素材指纹、历史 manifest 复用、COS 元数据和发布接口不变。横屏主画面保留 contain，背景 cover；修正旧 rotate 的 `rotw(iw)/roth(ih)` 错误画布范围。

## 时间轴与失败处理

- 原测试素材视频从 0.033008 秒开始，音频从 0 开始。旧 `PTS-STARTPTS` 把视频单独提前，并使 60 秒成片缩短约一帧。新链路维持共同时间轴，填充首帧，保留 1800 帧和完整 60 秒音频。
- 源输入 `-reinit_filter 0`，两路动态 scale/pad 先输出固定 RGBA 画面，再上传 GPU。避免中途横竖画幅切换重建 framesync 时丢弃缓冲帧。不要恢复五路直接上传且允许重建滤镜的原型，也不要在 OpenCL 输出后按帧序号重新计时。
- `TT_POST_GPU_COMPOSITOR_BACKEND` / `FB_PAGE_GPU_COMPOSITOR_BACKEND` 只接受 `cpu_legacy`（缺省）或 `opencl_fused_contain_v1`。启动时实际执行 OpenCL + 对应 NVENC 预检；失败直接阻止启动，不自动切回高 CPU 链路。
- 单任务串行；每输入解码线程及滤镜线程为 2，上线服务分别 CPUQuota=300%、Nice=5。两路总上限 6 核，为本机 8 核共享负载保留余量。
- health 返回 compositor_backend；新 manifest 记录后端，FB 额外记录 render_seconds。历史成片原样复用，不重做历史任务。

## 验证

Windows 本地 TT 95 项、FB 19 项通过。香港 T4 上两套引擎各通过 4 个逐帧测试：竖屏 30fps、横屏 25fps、途中分辨率变化、视频晚于音频一帧；总计 8 个场景，帧数和帧内容均匹配独立参考。

同一 60 秒源文件、各平台固定 recipe，串行测试。源 SHA-256：`b6bacb0925105a55e1d7e01140eb0f224a0cb5bba1dd41429b4a365ab613ad65`。

| 平台 | 旧版，4核预算 | 上线配置，3核预算 | 旧 / 新 CPU 核秒 |
| --- | ---: | ---: | ---: |
| TT | 91.214 秒 | 63.096 秒 | 354.342 / 185.733 |
| FB | 93.122 秒 | 63.819 秒 | 362.669 / 188.812 |

新版在更少 CPU 预算下，合成时间缩短约 31%，CPU 总计算量减少约 48%。这是单个固定样本的合成阶段，不含排队、下载、上传或平台发布，不能当作所有任务的端到端承诺。初期五路原型在同为 4 核时约 47–48 秒，但未通过变分辨率边界检查，不是最终上线版本。

两个最终成片均为 1800 视频帧、60 秒；TT 8,288,641 bytes，FB 38,722,882 bytes，完整音视频解码通过。测试命令：`python -m unittest scripts.test_random_gpu_compositor`、`scripts/check_random_gpu_timeline.py`、`scripts/benchmark_random_gpu.py`（均离线，不调用发布接口）。

## 部署与回滚

香港服务器 `43.154.250.89`。已验证代码：TT `ed76d7de5521a4ce80c718d18704dd2527a7ddc5`；FB `28ef0a1fa571dc17bfab9b6d0d54e840ffb071ea`。部署 unit 模板从本分支 GitHub 提交读取，不手工改生产业务代码。

1. CPU 使用现有维护控制器 `pause tt --apply` 和对应 gate，暂停新入口；保存并停止 FB prepare.timer，等待活动制作结束。保留发布账本。
2. 香港备份原单位、原指针与 manifest/发布账本摘要，位置 `/data/random-overlay-gpu/backups/20260907-before-fused`。
3. TT 新建 `/data/tt-post-gpu/random-current` 指向已验证 TT release。只为 `tt-gpu-publisher.service` 安装 `75-random-compositor.conf`；旧 `current` 和 `tt-gpu-direct-outro.service` 保持原版本。
4. FB `current` 指向 `/data/random-overlay-gpu/releases/28ef0a1fa571dc17bfab9b6d0d54e840ffb071ea`，安装 FB 对应 drop-in。
5. daemon-reload，启动两路 worker 及各自反向隧道。核对 GPU 与 CPU 隧道 health、后端、profile、配额和实际进程入口。
6. 恢复 TT 维护日志和 gate，恢复 FB prepare.timer。确认 TT 七个触发器恢复，`tt-triggers.json` 的 `restored=true`。不创建测试帖子或重放历史队列。

回滚须先重新暂停入口并排空：删除此次新增的两个 `75-random-compositor.conf`；FB current 原子恢复到 `/opt/fb-page-random-overlay/releases/59b42e24cd1e57b1209cb7addde3de7a8c98568b`；TT 原 current 一直保持 `/data/tt-post-gpu/releases/d05adad41a28383a5c9685e6b75c1c8581a2aa49`，移除 drop-in 后自然恢复该入口。daemon-reload，再启动两路 worker/隧道并恢复 CPU 原触发状态。只回滚代码和单位，不覆盖发布账本或新制作记录。

本次基线另发现 CPU TT readiness 对 systemd 的 `1month` 时间格式解析失败（automation_probe_unavailable），但实际触发器 active 且持续运行。它在 GPU 变更前已存在，本次不以 GPU 优化掩盖此独立问题。
