# GPU 迁移能力与实现边界核查

2026-11-04，独立 QA/源代码审查。环境事实来自本任务已完成的只读探测；候选能力来自下列主来源，均不等同于在当前节点完成验收。

## 当前基线

- 香港节点：Tesla T4，驱动 565.57.01；运行代码 `179596a9699393cd377c0e06a0bec94e171f14cd`，FFmpeg `n7.1-152-gd72536008a-20250113`，Python 3.10。
- 现有加速路径仍是 NVDEC → 主机 NV12/YUV420P 整理与拆平面 → OpenCL fit/composition → 主机 RGBA → NVENC。兼容路径另含软件源解码/缩放/输入色彩转换。
- 模板已用预解码 RGBA/NUT 缓存，不应把原始 PNG/VP9 的重复解码算成当前每次制作必有的负担。
- 当前音频命令为 `aresample=48000:async=1:first_pts=0,apad` 加软件 `aac`，输出由 FFmpeg MP4 muxer 和 `+faststart` 完成。
- 现有环境未安装 CuPy、PyNvVideoCodec、PyAV、PyTorch、CUDA Toolkit/`libcufile`，且没有加载 `nvidia_fs`。这说明目前没有现成接入，不代表硬件或理论计算能力不支持。实验依赖仅放入隔离环境。

## 逐项结论

| 项目 | 技术能力 | 当前可用实现/证据 | 本次需要验证的边界 |
| --- | --- | --- | --- |
| 视频解码/编码 | T4 的 NVDEC/NVENC 可承担支持的格式 | 当前已使用；新 Python codec 必须匹配驱动/API | 用运行时能力查询和实际源片验证，不能依据最新 SDK 的整代 GPU 总表假定 T4 支持所有格式 |
| 图像缩放/颜色/图层计算 | 可由 CUDA/OpenCL 承担 | 当前已有 OpenCL 内核，CUDA 是可研究的连续显存方案 | 保留配方、色彩范围、alpha、时间线；重写内核需完整画面比较 |
| 音频采样 DSP | 可由通用 GPU 实现 | CuPy 提供 `cupyx.scipy.signal.resample_poly` 等；cuSignal 已归档并将功能迁入 CuPy | 算子不自动继承 FFmpeg 的 `async`、首 PTS、补静音和跨块状态 |
| AAC-LC 编码 | 通用 GPU 可承担大量 codec 计算，NVENC 不提供 AAC 编码 | 找到 `torch-aac 0.7.2` 候选；官方描述含 GPU 计算及 CPU 位打包 | 真实双声道 48 kHz/192 kbps、delay/flush、位流兼容性、质量、内存和CPU残留必须实测 |
| AAC/其他压缩音频解码 | NVDEC 不承担 AAC | 目前接入路线是 PyAV/FFmpeg 软件解码；没有在本审查中验证完整 GPU 压缩音频解码器 | 不能把 PCM 在 GPU 上的处理声称为压缩位流解码已迁移；仍应继续验证可用候选，不以搜索未发现推导理论不可能 |
| SHA-256 | 可编写 GPU 实现 | 标准有明确顺序状态依赖，可并行不同文件/独立消息 | 同一文件每块必须继承前态；并行树哈希不能冒充原 SHA-256 |
| MP4 封装计算 | 索引、偏移、长度、表生成等可编写 GPU 实现 | 现有 `movenc` 是主机 muxer，没有配置开关可以直接切换整条 MP4 封装到 GPU | 要区分 GPU 元数据算法与完整 mux/finalize；B 帧、edit list、64位偏移及原始 packet 等价必须验证 |
| 直接存储传输 | 合适软硬件可通过 DMA 避开主机中转 | GDS/cuFile 是候选，但当前节点未配置 | 需核对存储/文件系统/驱动拓扑并跑实际 direct path；只安装包或出现 API 成功不证明没有 fallback |
| 主机控制 | GPU 算法不替代当前 OS/SDK 的所有职责 | 文件生命周期、外部请求、驱动调用、账本和任务管理仍由主机执行 | 将管理开销与逐像素/逐样本计算分别测量，不宣称零 CPU |

## AAC 候选的具体情况

`torch-aac 0.7.2` 的 PyPI 发布来源指向提交 `5dfc83fa446292da0520185e3da24374fdfd9922`。它宣称支持 T4、AAC-LC、16/44.1/48 kHz、单/双声道和 48–320 kbps；本次已对实际 T4 48 kHz 双声道进行隔离测试，结果见下方更新，未通过完整生产验收。

项目明确区分两条路径：真实编码将 GPU 上的变换、量化、码本和 Huffman 结果交给 CPU C BitWriter/ADTS 组装；`DifferentiableAAC` 是不输出压缩位流的可微模拟。因此它可能解决“大量 AAC 计算迁移”，但不能原样称为“完整无 CPU AAC 编码”，也不是压缩 AAC 解码器。

其 `pyproject.toml` 标为 Alpha，依赖 PyTorch、NumPy、SoundFile，许可证为 Elastic-2.0。许可证需按实际内部使用与对第三方提供服务的边界处理；既不假定它是 MIT，也不仅因这一名称就自动判定内部制作使用不可用。保留原许可和来源，不将新候选写入生产依赖前先做隔离验证。

## MP4 迁移的准确口径

当前 `movenc.c` 会生成轨道 sample tables，计算 moov 大小，并在 faststart 中移动媒体数据和修正 offsets。一个 GPU prefix-sum 或 offset-add 内核能证明该计算被迁移，但仍不能证明 packet interleave、DTS/PTS、box serialization 和完整文件生成已迁移。

可以按实际代码分阶段接入：GPU 计算/生成元数据，主机管理 IO 和提交；但报告中应写清仍由 movenc 承担的部分。无论放在哪执行，`stco`→`co64` 扩展引发 moov 大小变化的二次偏移、B 帧 ctts、edit list 和轨道时间基都不可省略。

## 顺序 SHA-256 的准确口径

FIPS 180-4 §6.2.2 规定消息块按顺序处理，当前块使用上一块的八个状态字。GPU 可以保留这一状态在设备端顺序计算，或并行多个文件；但是 `SHA256(chunk1) || SHA256(chunk2)` 再哈希不是原文件的 SHA-256，不能用于替代已有源身份、COS key、manifest 和校验协议。

## 主来源

- [NVIDIA Video Codec SDK](https://developer.nvidia.com/video-codec-sdk)：NVDEC/NVENC 面向视频格式的能力。
- [NVIDIA PyNvVideoCodec 系统要求](https://docs.nvidia.com/video-technologies/pynvvideocodec/read-me/system-requirements-common.html)：候选依赖与运行平台；仍需按实际选定版本验证。
- [CuPy resample_poly](https://docs.cupy.dev/en/stable/reference/generated/cupyx.scipy.signal.resample_poly.html)：多相 FIR 重采样 API。
- [RAPIDS cuSignal 迁移公告](https://docs.rapids.ai/notices/rsn0032/)：cuSignal 功能迁移到 CuPy。
- [torch-aac PyPI](https://pypi.org/project/torch-aac/)：真实位流/可微模拟区别、GPU/CPU 阶段及候选配置声明。
- [torch-aac 精确发布 pyproject](https://raw.githubusercontent.com/VerdaAI/torch-aac/5dfc83fa446292da0520185e3da24374fdfd9922/pyproject.toml)、[许可](https://raw.githubusercontent.com/VerdaAI/torch-aac/5dfc83fa446292da0520185e3da24374fdfd9922/LICENSE)：版本、依赖、开发状态和条款原文。
- [FFmpeg 7.1 AAC 编码实现](https://github.com/FFmpeg/FFmpeg/blob/n7.1/libavcodec/aacenc.c)：既有 codec 的状态、声道和位流工作。
- [FFmpeg 7.1 MP4 muxer](https://github.com/FFmpeg/FFmpeg/blob/n7.1/libavformat/movenc.c)：容器、offset 和 faststart 的完整实现边界。
- [NIST FIPS 180-4](https://nvlpubs.nist.gov/nistpubs/fips/nist.fips.180-4.pdf)：标准 SHA-256 定义，尤其 §6.2.2。
- [NVIDIA GDS 设计](https://docs.nvidia.com/gpudirect-storage/design-guide/index.html)、[GDS O_DIRECT 条件](https://docs.nvidia.com/gpudirect-storage/o-direct-guide/)：直接传输与兼容 fallback 的条件。

## 本次审查状态

2026-11-04 追加：已在隔离 venv 执行微型 GPU SHA/MP4 测试。SHA 故障回归 7/7、最终 MP4/Annex B 回归 14/14 通过；详见 `sa-code-review.md`。根代理实现的 CUDA MP4 路径包含 Annex B→AVCC、交织、sample tables、offsets、box serialization，不调用 FFmpeg muxer；主机仍供应压缩包/codec 描述、计划 box 结构并执行文件 IO/提交。此实现范围为连续 H264 + 可选 AAC 轨道，不是任意 MP4 格式的通用替代。

音频子代理报告 `audio-results/final-audio-contracts.json`：DSP 12 项、native PCM/时间基/无音轨/断点拒绝 4 项、bitpack 5 项通过；真实 AAC 96 次 pack 字节等价；GPU ADTS 6 个配置及 95 帧真实候选重建字节等价。早期 AAC 样本发现尾部缺 768 samples；随后补帧与质量实验仍未达到完整生产验收。真实对白 4 秒基线 SNR 44.46 dB，候选 uniform192 为28.97 dB，M/S192 为35.20 dB；带宽/实际码率不同，不能只凭合成信号 SNR 推断普遍听感。完整编码还含 CPU block-switch/ICS/section/scalefactor 与部分 Huffman 路径，压缩音频解码仍在 CPU。不能把 bitpack/ADTS 成功说成完整 AAC 已迁移。

未修改服务、生产配置或生产数据，未暂停任务或重启。当前候选能力、独立组件测试和生产部署状态必须分别陈述。

## GPUDirect Storage 当前环境核查

2026-11-04 通过 SSH 只读查询 `findmnt -T /data`、`lsblk`、sysfs、`lspci -nnk`、`uname -r`、内核配置、`ldconfig -p`、`lsmod` 和 RPM 列表：

| 项目 | 实测事实 |
| --- | --- |
| 数据盘 | `/data` 位于根挂载 `/dev/vda1`，ext4，`rw,relatime`；无独立 NVMe 盘暴露 |
| 存储驱动 | `/sys/class/block/vda/device/driver` 为 `virtio_blk`，PCI 设备为 Red Hat Virtio block device |
| 网络 | Red Hat Virtio network device；`/sys/class/infiniband` 不存在，未发现可用 RDMA 网卡 |
| 内核 | CentOS 8，`4.18.0-348.7.1.el8_5.x86_64`；`CONFIG_PCI_P2PDMA is not set` |
| GPU/驱动 | Tesla T4；NVIDIA UNIX Open Kernel Module `565.57.01` |
| GDS 组件 | 系统动态库索引无 `libcufile`；无已加载 `nvidia_fs`、`/proc/driver/nvidia-fs`、`/etc/cufile.json`、gdscheck/gdsio 或对应 RPM；未发现系统 CUDA 安装目录 |

结论：当前挂载路径没有可直接启用并验证的端到端 GDS 通路。缺少 cuFile/验证工具只是其中一层；当前普通 virtio 块设备也没有提供所需的存储到 GPU DMA 集成，不能以安装 Python 包代替存储/内核支持。

这不是“T4 不支持 GDS”：NVIDIA [安装排障指南 §4.2](https://docs.nvidia.com/gpudirect-storage/troubleshooting-guide/index.html) 明确列出 T4 可用经典 GDS。新 NVMe PCI P2PDMA 路径有另一组 GPU/内核/驱动条件，当前 T4 和未启用 P2PDMA 的内核不满足该路径；不能因此排除经典方案。[GDS 架构说明](https://docs.nvidia.com/gpudirect-storage/overview-guide/)也说明兼容模式会通过主机内存中转，API 成功本身不证明直传。

后续需要云平台提供受支持的 NVMe/存储驱动或 RDMA 文件系统通路，选择匹配内核/驱动的 GDS 版本后，再以 `gdscheck` 和实际读写统计排除 fallback。当前未执行端到端直传实验，未安装系统模块、改驱动、改 mount 或重启。
