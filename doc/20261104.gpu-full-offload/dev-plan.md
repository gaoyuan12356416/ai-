# 开发计划

## 开发范围

以当前香港节点实际繁忙的 FB H264/CQ21 制作为第一条完整接入路径；共享文件哈希覆盖 FB、TT、X 制作。其它平台不同编码合同须单独适配验收，不能因为共享一个 GPU 就直接套用 CQ21。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| GPU SHA、MP4、子进程与FB接入 | root | random_gpu、fb_gpu | 组件通过，完整30秒回归中 |
| NVDEC/CUDA/NVENC与缓存图层 | video_gpu_native | native_video.py/.cu | 8bit主路径通过，颜色/特殊源补测 |
| 音频DSP、AAC候选与兼容验证 | audio_gpu | gpu_audio.py | GPU DSP通过，CPU codec保留；时钟洞兼容中 |
| 独立QA与GitHub部署准备 | qa_gpu_offload | scripts、doc、installer | 组件回归通过，整体未放行 |

## 编译 / 构建命令

```bash
python -m compileall -q features/random_gpu
git diff --check
```

## 风险与依赖

隔离环境不覆盖线上Python、FFmpeg或驱动。锁定CuPy13.6.0、NumPy2.2.6、PyNvVideoCodec2.2.3、PyAV16.0.1，CUDA12.6用户态runtime/NVRTC wheels。1.0.2丢失时间戳、2.0.0忽略CQ的实际失败均已排除；不得以能导入代替真实编码参数核验。

主代理负责基线、GPU SHA、封装与集成；video_gpu_native负责显存常驻视频；audio_gpu负责音频和AAC候选；qa_gpu_offload独立核查能力、用例和代码。

先组件设备验证，再端到端样本与并发/取消/恢复，最后等待用户暂停确认后切换。设备不可用的测试不能记为通过。不可把未达质量的全GPU AAC实验投入生产。

## 完成记录

组件设备证据与QA统计见test-report/各validation文档，尚无生产部署记录。GPU SHA串行链正确但墙钟比CPU慢，须计入完整性能，不隐瞒offload成本。GDS当前virtio存储/驱动条件不满足，保留主机IO；不擅自换内核/磁盘基础设施。
