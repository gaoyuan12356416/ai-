# 测试报告

## 结论

代码回归、固定输入 GPU 制作、混合分辨率时间轴、完整解码与音频对齐通过；生产记录见 deploy.md。性能数字限定随机模板制作阶段，不代表下载、排队、音频处理在内的总任务时间。

## 自动回归

330 项通过，耗时 18.474 秒。范围：新帧链路、现有 compositor v2、GPU runtime、吞吐恢复、远端客户端和媒体流程。py_compile 与 git diff --check 通过。

## 固定 30 秒样片

源 SHA：`66ef7cd6233b27514e962140a8fbca099fe2896b343c93d189e9fe7a1fdd9240`；配方 SHA：`88ef90b77a3768948e61cebd625d0a12aaf743f1dd3bdc5b23ec2f69593c5011`。

| 路径 | 墙钟秒 | 累计 CPU 秒 | 平均 CPU 核数 |
| --- | ---: | ---: | ---: |
| 原链路 | 19.726 | 87.929 | 4.457 |
| 预解码缓存试验 | 9.724 | 26.450 | 2.720 |
| 最终 NVDEC/CUDA/NVENC | 8.400 | 8.007 | 0.953 |

最终版本 CPU 计算量降低 90.894%，墙钟降低 57.417%（约 2.35 倍速度）。对照 SSIM 0.991991，输出 900 帧 / 20,357,789 字节。抽帧检查几何、颜色和透明边缘通过；GPU 色彩转换和有损编码路径不同，成片不承诺逐像素或文件哈希相同。

## 时间轴、像素和整片

- 130 秒横屏片头 → 竖屏剧集、25fps 输入：3250 个源帧 PTS/尺寸逐帧匹配软件解码；17 个抽样 NV12 帧像素零差异。
- 从 120 秒寻址：250 源帧 PTS/尺寸匹配，3 个抽样像素零差异。
- 30fps 横屏：300 源帧 PTS/尺寸匹配，4 个抽样像素零差异。
- 全部 18 项缓存完成原始逐帧 RGBA/PTS/duration 校验；两个选中动画非整数起点、跨循环各 90 帧 MD5 与 FFmpeg 完整循环一致。
- 最终 120+10 秒分片、拼接和音频流程：63.077 秒墙钟 / 36.324 CPU 秒；3900 帧、130 秒，H.264 High、720×1280、30fps、BT.709 limited、AAC，完整解码通过。
- 子进程峰值 RSS 664204 KiB；共享设备采样峰值显存 522MiB，GPU 43%，首轮 swap 增量 0。GPU 采样包含同时运行的生产规格化工作，不能当作该样片独占利用率。
- 音频 10/60/115 秒位置的相关性 0.999803 / 0.999892 / 0.999934，估算偏移均 0。源拼接音频的少量时间戳舍入告警仅出现在诊断 PCM 导出；最终媒体完整解码无错误。
- 最终 SHA：`b528c8fcb5574f2639be6f82a474171c0eabd0ec963f7a1d524aa5a5b9b8dde4`。
- 已完成结果复用：3.384 秒 / 0.241 CPU 秒，输出 SHA 不变。该次基准总判定 false 的唯一资源门槛为主机 swap 增加 786432 字节；媒体契约和复用均通过，不把系统级小幅变化伪报为缓存重渲染。

## 预检与失败试验

最终真实服务账号预检通过：18 项缓存、固定包版本、模型/FFmpeg 指纹、NVDEC/CUDA/NVENC 实际样片、app import；cuda_tested=true。实际 systemd 沙箱启动结果见部署记录。

NVIDIA demux/Seek、全 I 帧参数、旧缓存时间量化、seek+loop 尾段重复等失败试验已修复，见 bugs/BUG-001.md。旧 OpenCL 混合分辨率对照存在时间偏移，不能作为整片正确参考；使用原素材 PTS 和解码像素独立验证。

## 证据

HK 私有实验：`/data/drama-synthesis-gpu/experiments/frame-pipeline-20260915/`；基准：`/data/drama-synthesis-gpu/work/benchmarks/frame-native-final-30s-20260915/` 与 `frame-native-final-mixed-20260915/`。

首轮完整基准保存在 `native-mixed-final-first-report.json`，复用基准保存在对应 benchmark/report.json。其他关键证据：`native-frame-qa-nv12-final.json`、`native-frame-qa-seek120.json`、`native-frame-qa-30fps.json`、`final-30s-comparison.json`、`final-audio-and-decode.json`、`preflight-final.json`。
