# 开发计划

## 开发范围

FB、X、TT随机模板制作均已完成候选接入，分别保留原平台编码合同；共享文件哈希覆盖三平台。FB为H264/CQ21/192k音频，X为原H264/CQ21及动态文件大小预算/128k音频，TT保留当前HEVC及可选H264的P6/HQ/VBR/128k音频。CPU profile、source-direct和outro等原模式不改合同。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| GPU SHA、MP4、子进程与FB接入 | root | random_gpu、fb_gpu | H264/HEVC封装组件及FB真实30秒全链通过；最终发行SHA与生产lease修复融合后复验 |
| NVDEC/CUDA/NVENC与缓存图层 | video_gpu_native | native_video.py/.cu | 主路径、多类高位深/特殊源、时间线及兼容采样实测通过；未覆盖边界见video-validation |
| 音频DSP、AAC候选与兼容验证 | audio_gpu | gpu_audio.py | 192k扩展14项、128k扩展15项通过；真实30秒时钟洞兼容通过；完整GPU AAC质量不合格，不启用 |
| X适配与独立QA | qa_gpu_offload | x_posts、scripts、doc | 原动态码率预算、CQ修复、128k音频及两组真实2秒完整链通过 |
| TT适配与HEVC色彩 | audio_gpu、video_gpu_native | tt_gpu、color_contract、native_video | 双codec参数读回及真实2秒链通过；HEVC固定输出色彩context修复并集成，原生codec不改为H264 |
| GitHub与部署准备 | root、qa_gpu_offload | installer、deploy文档 | 尚未部署；最终SHA、干净依赖环境、服务用户与并发/取消验收以最终报告为准 |

## 编译 / 构建命令

```bash
python -m compileall -q features/random_gpu
git diff --check
```

不依赖GPU的音频/TT直接回归：

```bash
python -m unittest scripts.test_gpu_audio scripts.test_tt_native_adapter scripts.test_tt_gpu_worker scripts.test_native_video scripts.test_pipeline_executors
```

这些CPU单元测试不能代替 `verify_gpu_audio*.py`、`verify_tt_native*.py`、`verify_hevc_color*.py` 的真实设备/媒体验证。已完成证据见audio-validation、tt-native-validation、video-validation及最终test-report；不要把重复运行的数量相加为新的最终验收数。

## 风险与依赖

隔离环境不覆盖线上Python、FFmpeg或驱动。锁定CuPy13.6.0、NumPy2.2.6、PyNvVideoCodec2.2.3、PyAV16.0.1，CUDA12.6用户态runtime/NVRTC wheels。1.0.2丢失时间戳、2.0.0忽略CQ的实际失败均已排除；不得以能导入代替真实编码参数核验。

主代理负责最终基线、GPU SHA、封装与集成；video_gpu_native负责显存常驻视频与HEVC context生命周期；audio_gpu负责音频、AAC候选和TT参数适配；qa_gpu_offload负责X适配并独立核查能力、用例和代码。正式版本必须保留实际生产已上线的FB lease overlay，不能用旧179596a发布目录覆盖这些修复；具体来源和启动路径见deploy。

先组件设备验证，再端到端样本与并发/取消/恢复，最后等待用户暂停确认后切换。设备不可用的测试不能记为通过。不可把未达质量的全GPU AAC实验投入生产。

## 完成记录

用户最新安排：本轮开发完成后停止，部署明天再进行；本轮不stage、不激活、不切换生产。下列最终发行验收/部署事项记录为后续工作，不据此继续扩大本轮执行范围。

组件设备证据与QA统计见test-report/各validation文档，尚无生产部署记录。FB真实30秒链、X两组2秒链和TT双codec2秒链已经完成，不能继续列为“尚未适配”；同时这些结果不代替融合后最终SHA验收或长片并发容量证明。

CPU明确保留压缩音频解码及AAC编码、已知时钟/声道异常的FFmpeg DSP兼容、T4/SDK未合格视频格式的源解码、压缩包解封装/少量元数据控制、文件及网络IO。音频特殊兼容支路事前按明确原因选择，CUDA故障不静默降级。完整GPU AAC的语法/部分Huffman实现和真实对白质量尚不合格；GPU bitpack/ADTS组件成功不代表完整codec上线。

GPU SHA串行链正确但墙钟比CPU慢，须计入完整性能，不隐瞒offload成本。GDS当前virtio存储/内核/驱动通路未具备，保留主机IO；不能把兼容模式当直传，也不擅自更换基础设施，详见capability-review与根测试报告。
