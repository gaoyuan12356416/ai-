# 原生 GPU 视频路径与验证

本页只覆盖视频。测试目录为香港 GPU 的 `/data/pipeline-dispatch/evaluations/gpu-full-offload-20261104`，简称 `E`。测试读取冻结源片和已验证缓存，没有暂停、重启、修改生产任务或替换生产服务。提交后的精确 SHA 验证与部署回执见主测试、部署文档。

## 实现边界

主路径为 NVDEC → CUDA 平面采样 / 色彩转换 / Lanczos 缩放 → CUDA 冻结模板合成 → NVENC。解码面在下一次 Decode 前复制到独立 GPU 面，防止 SDK surface lease 回收；像素不经过 CPU。四层已验证 NUT 缓存由 CPU 解封装，直接从 packet host pointer 上传 CUDA；短层驻留显存，长层使用两个缓冲区。CPU 仍负责压缩包解封装、时间戳、调度、小型参数、编码压缩字节和文件读写。

T4 不支持的编码格式由实际 `GetDecoderCaps` 事前选择 PyAV CPU 解码。原始平面一次上传后，色彩、缩放、合成、编码仍在 GPU。CUDA 执行、解码、编码失败不会触发隐藏 CPU 重做。

例外是交错扫描：T4 能解码，但 PyNvVideoCodec 2.2.3 内部固定 Adaptive 去交错且 Python 没有 Weave 控制。旧流程保留原场内容；实测 NVDEC Adaptive 与 CPU Weave 的 92,160 个样本有 44,463 个变化、最大差 223。因此按 field_order 事前选 CPU 解码，避免擅自改变成片效果。不是 GPU 硬件能力不足。

采样支持 NV12、P016 的 10/12/16 位高位对齐、原生三平面、CPU 解码后的整数/浮点/大小端/调色板/位图/Bayer。BT.601、BT.709、BT.2020、SMPTE240M、FCC 的源矩阵在 GPU 处理；保持旧流程不做 HDR tone mapping 的行为。Bayer 使用 GPU 双线性去马赛克，已验证已知色值，未宣称与所有旧 swscale 纹理逐像素相同。

## 时间线与图层

- 以容器 format.start_time 为共同零点，按精确 Fraction 对齐 `fps=30:start_time=0`。输入 VFR、低帧率、首帧延迟、末端视频短于音频均按实际 PTS / duration 处理。
- `-t` 的编码帧上限采用近邻舍入，不使用 ceil。实际旧 NVENC 测试：0.01 秒为 1 帧，0.10001 秒为 3 帧，1.00001 秒为 30 帧。
- 每个编码 AU 使用 SDK 返回的真实 PTS；验证完整 PTS 集合后，由官方 FFmpegMuxer 同款最小 decode delay 公式生成 DTS。保留 B 帧，不猜测 B 帧输出顺序。
- `.packets.json` 输出 AU 文件偏移、size、PTS、DTS、duration 和 sequence header。H264/HEVC 的独立验证流头位于 AU 偏移之前，供 GPU MP4 封装器单独构造 avcC/hvcC。
- 7 fps NUT 图层连续循环生成 60 个 30 fps 输出，与旧 FFmpeg `stream_loop + fps` 原始 RGBA 逐字节一致。

旧主路径 qualified 源沿用 OpenCL 的采样坐标；旧元数据不合格的源使用 CPU swscale 的色度坐标规则，包括相邻两个 RGBA 像素复用半宽色度。选择沿用原 `gpu_pipeline.qualification` 元数据判断，不恢复旧 `source_qualification` 全文件关键帧 CPU 扫描。动态尺寸或动态标签变化导致的旧扫描分支不保证逐像素重现，但输入动态尺寸已实测正确解码/制作，几何与画面质量单独验证。

## NVENC 合同与已修 SDK 问题

依赖固定 PyNvVideoCodec 2.2.3。旧 1.0.2 缺可靠输出 PTS，2.0.0 的 CQ 字符串没有真实生效，均不作为生产候选。

1. SDK 构造参数同时提供 CQ 和非零码率时，会把 averageBitrate/maxBitRate 清零。实现先省略 CQ 构造并核对实际初始 budget，再调用正式 Reconfigure 保留 budget 并设置 CQ。getter 是构造快照，回执分别记录 initial / applied_reconfigure，不假称 getter 是重配后的实时值。QA 四组真实复杂画面输出证明 CQ 和预算都实际生效，另见 X 验证。
2. `colorspace='bt601'` 旧配置接口会隐式启用 full range 却不正确标记 SPS。禁止这个选项。H264 默认实测为 limited BT.601，保持不变。
3. HEVC 无 VUI 默认 ABGR 转换实际为 limited BT.709，不能仅靠 MP4 colr 标签修正。HEVC 使用 SDK 官方 `source_av_format_context`，参考为自生成、固定 30 fps、limited BT.601 的 64×64 黑片。context 生命周期覆盖完整编码并在退出时清理，不读取用户源色彩作输出合同，不把临时指针写入 sidecar。SDK 以 context 帧率优先，所以参考帧率强制校验 30/1。
4. 开启 global_header 与旧 MP4 编码模式一致，避免 SPS/SEI 额外开销影响 NVENC 码率控制。没有为了迁移把 TT HEVC 改成 H264；TT、X 编码参数各按原平台适配器保留。

已知色块对 H264 和修正后的 HEVC 验证：红 (Y,U,V)=(81,90,240)，绿=(145,54,34)，蓝=(41,240,110)，黑=(16,128,128)，白=(235,128,128)。HEVC 未修正时红为 (63,102,240)，已作为被拒绝候选留证。

## 实际验证结果

| 项目 | 结果 | 证据相对 E |
|---|---|---|
| 冻结真实 720p / 4 层缓存 / 旧 179596a 原始 RGBA | RGB 最大差 1，PSNR 60.255641 dB，95.4% 全样本相同 | `native-video/formats/final-raw-pixels.json` |
| 同源同配方 6 秒 H264 原始旧成片对比 | 180 帧，完整解码；PSNR 40.5611 dB，SSIM .989536 | `native-video/native-real6-final.h264` 与 `baseline-real6.mp4` |
| HEVC10 BT.2020 / PQ 源，对旧 CPU-fit 原始 RGBA | PSNR 39.229184 dB，RGB MAE 1.287/.922/.889；无 tone mapping | `native-video/formats/final-raw-pixels.json` |
| Full-range CPU-fit 兼容采样 | PSNR 42.4921 dB，画面形状与透明区域一致 | `native-video/matrix/compat-pixels-v2.json` |
| SAR 2:1 CPU-fit 兼容采样 | PSNR 41.4115 dB，几何一致 | 同上 |
| P010 NVDEC 原始值 | 与 FFmpeg CPU p010le 的 92,160 个样本完全相同 | `native-video/decode-pixel-contract.json` |
| 7 类时间线 | 20fps、7fps、VFR、首帧延迟、音频长尾、full range、SAR 的被选输入帧与 FFmpeg 一致 | `native-video/matrix/timeline-matrix.json` |
| 7 类格式完整制作/重解码 | H264422、AV1、RGB FFV1、交错扫描用 CPU 解码；HEVC12、HEVC44410、动态尺寸用 NVDEC；6/12 帧均完整 | `native-video/formats/formats.json` |
| 其它格式 | H26410 CPU、HEVC10 / HEVC444 NVDEC，各 6 帧完整制作/解码 | `native-video/matrix/*-output.validation.json` |
| 8 类 GPU 通用采样 | PAL8、RGB4、monob、monow、GBRPF32LE、RGBA64BE、BayerBGGR8、BayerRGGB16LE 全部通过已知色值 | `native-video/sampler-formats.json` |
| 初始 >4K / odd 420 尺寸 | 5120×192 HEVC 使用 NVDEC；193×321 VP9 因 SDK surface 未合格事前选 CPU 解码，之后全 CUDA/NVENC；各 1 帧完整严格重解码，测得进程显存峰值分别 218 / 220 MiB | `native-video/geometry/geometry.json` |
| TT HEVC 2 秒，context 修正 | 60 帧、PTS 不变、音频 PCM 不变、完整解码，tv/bt470bg；PSNR 38.804 dB、SSIM .972120 | `tt-pipeline-hevc-color-2s/result.json` |
| 本地纯视频回归 | 14 项通过：时钟、lease、P016 stride、CQ/budget、HEVC context 成功/取消生命周期、H264 不创建 context | `scripts/test_native_video.py` |

注意 `compat-pixels.json`、带 legacy colorspace 的早期 native-real6 以及没有固定 context 的 HEVC 是排障中被替代的结果，不能当最终验收。

## 真实 30 秒性能

同源、同冻结配方、同四缓存层、同 FB H264 合同。旧 baseline 使用正在运行的 179596a 原 `build_command`，不是旧诊断文档中的其它编码命令。

| 指标 | 旧路径 | 新完整链路 |
|---|---:|---:|
| CPU user + system | 52.867 秒 | 15.579 秒 |
| 墙钟 | 67.391 秒 | 30.016 秒 |
| GPU 显存峰值 | 704 MiB | 555 MiB |
| 子进程 RSS 峰值 | 662,200 KiB | 395,856 KiB |

旧证据 `native-video/baseline-real30.performance.json`；新证据 `real-fixture/native-integrated30-final.acceptance.json`。新片 900 视频帧、1,408 AAC 包、30 秒准确、严格完整解码通过。原始源音频存在时间洞，因此该样本的音频 DSP 使用已验证 CPU 兼容支路，不能称这组测量为“所有音频运算均在 GPU”。小型 nvidia-smi 子进程监控开销含在 CPU 统计中。

## 尚未宣称覆盖的边界

最终精确 SHA 的全链、取消/故障及并发验收由主验收继续。现测试覆盖不代表所有 FFmpeg 可接受媒体的穷举。NVDEC 构造的 maxwidth/maxheight 对普通片保持 4096，初始更大时按初始实际尺寸提高，并限制在实际 device caps 内；已验证 5120 宽，未实测完整 8K 画面。运行中动态尺寸超过这个初始 allowance 仍是未覆盖边界。奇数尺寸 4:2:0 已保留 CPU 仅解码兼容支路，不把当前 SDK surface 描述未合格说成 T4 硬件不支持。GPU 失败不静默重跑旧管线，避免重复长耗时及掩盖错误。
