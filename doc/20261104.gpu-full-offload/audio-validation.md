# 音频迁移验证记录

## 结论与边界

2026-11-04 在香港 Tesla T4 的独立环境完成真实 CUDA 验证；未暂停、drain、重启或改动生产任务/服务。生产代码基线 `179596a9699393cd377c0e06a0bec94e171f14cd`。

* PCM 数值转换、48 kHz 重采样、单/双声道矩阵、按输出时钟裁断和补静音已有 CUDA 实现及独立 FFmpeg 对照。连续时钟、已列出的采样率/声道夹具通过。它不是完整音频编码器。
* 可变长度码字的前缀和、位打包、ADTS 头/包长/包位置计算已有 CUDA 实现。真实候选 AAC 的位打包、ADTS 重建均逐字节等价。
* `torch-aac 0.7.2` 可以在 T4 上生成严格 FFmpeg 解码通过的真实 AAC-LC；其原始无补偿调用会短尾，默认音质、码率控制、短窗/perband 路径存在问题。尝试首尾补偿、两种量化模式、M/S、提高码率和局部形状修复后，真实对白仍未达到原编码器的质量证据。完整 GPU AAC 编码未获生产资格。
* AAC 压缩源仍由 CPU PyAV/FFmpeg 解码。没有把 FFT/MDCT、可微模拟或 CUDA 张量当成已完成 GPU AAC 解码。未找到并实测通过可直接替换现有源格式的 GPU AAC 解码器。
* 尚未实现的 ICS/section/scalefactor 字段生成、block-switch 状态机、短窗/M/S 的 Huffman **算法上可以迁 GPU**；当前缺少完整适配和验证，不是 GPU 算力/指令能力不允许。已验证的独立算子不能被记成完整编码器接入、完整制作链通过或生产部署通过。

## 隔离环境与复现

服务器环境根目录：`/data/pipeline-dispatch/evaluations/gpu-full-offload-20261104`。

| 组件 | 实测版本 |
| --- | --- |
| GPU / 驱动 | Tesla T4 / 565.57.01 |
| CuPy | cupy-cuda12x 13.6.0 |
| NumPy / PyAV | 2.2.6 / 16.0.1 |
| PyTorch | 2.6.0+cu126，官方 cu126 wheel |
| torch-aac / soundfile | 0.7.2 / 0.13.1 |
| CUDA runtime / nvrtc wheel | 12.6.77 / 12.6.85 |

所有依赖只安装到上述目录的 `venv`，未升级驱动、未改系统包或生产 Python 依赖。pip 从 23 升至 25.3 仅用于解决隔离环境从官方 Torch wheel 索引解析 typing-extensions 名称失败。安装脚本经检查后执行；实际 C BitWriter 编译可用。安装日志 `torch-install.log`。中断后核查无残留安装进程。

代码在隔离目录 `audio-code`，结果在 `audio-results`。命令：

```bash
venv/bin/python audio-code/scripts/verify_gpu_audio.py --aac --output audio-results/final-audio-contracts-exact.json
venv/bin/python audio-code/scripts/qualify_gpu_aac.py --source /data/pipeline-dispatch/diagnostics/gpu-cpu-20261103/baseline.mp4 --output audio-results/aac-quality-adapted.json
venv/bin/python audio-code/scripts/verify_gpu_audio_track.py --source /data/pipeline-dispatch/diagnostics/gpu-cpu-20261103/baseline.mp4 --output audio-results/audio-track-expanded.json
venv/bin/python audio-code/scripts/verify_gpu_audio_track.py --source /data/pipeline-dispatch/diagnostics/gpu-cpu-20261103/baseline.mp4 --audio-bitrate 128000 --output audio-results/audio-track-compat-expanded-128k.json
venv/bin/python audio-code/scripts/verify_gpu_audio_compat.py --source real-fixture/source.mp4 --duration 30 --output audio-results/real-30s-compat.json
```

另在共享 worktree 执行 `python -m unittest scripts.test_gpu_audio`（4 项通过）和 `python -m py_compile features/random_gpu/gpu_audio.py scripts/verify_gpu_audio.py scripts/qualify_gpu_aac.py scripts/verify_gpu_audio_track.py`。

## PCM DSP 的实测结果

参考命令采用现有音频语义：`aresample=48000:async=1:first_pts=0,apad`、双声道和显式 `-t`。GPU 采用全局输入坐标、连续分数相位、分块 halo 与边界反射，不把每个块当成新的独立信号。

预先设定的数值检查：正弦组合夹具内部 SNR≥50 dB、全信号 SNR≥60 dB、最大绝对误差≤0.01，样本数精确相等；不同块长结果逐样本相同。这是算子夹具容差，不能解释为任何实际源或端到端听感已经通过。

| 输入 | 输出样本数 | 全信号 SNR | 最大绝对误差 | 8192/10001 分块一致 |
| --- | ---: | ---: | ---: | --- |
| 48 kHz 双声道 | 48000 | 零差异 | 0 | 是 |
| 48 kHz 单声道 | 48000 | 零差异 | 0 | 是 |
| 44.1 kHz 双声道 | 48000 | 81.45 dB | 0.001477 | 是 |
| 16 kHz 单声道 | 48000 | 67.09 dB | 0.003072 | 是 |
| 96 kHz 双声道 | 48000 | 89.88 dB | 0.000302 | 是 |

补充：±0.25 s 起点、1.5 s 输出、无音频生成 0.5 s 精确静音均通过。小数时长按最近采样点取整，与 FFmpeg 实测一致：1.00001→48000、1.00002→48001、1.00003→48001、0.100001→4800。原先 `ceil` 的 1 样本误差已修正。不足半个样本的时长没有视频业务意义，显式拒绝，未模仿 FFmpeg 极短 `-t` 的特殊行为。

PyAV helper 原生 PCM 类型/44.1k/mono 保留、Matroska 粗时基 +0.25 s、无音轨均通过；100 ms 中间 PTS 空洞被明确拒绝。粗时基允许一个容器时间刻度的表示舍入，不掩盖真正空洞。中间空洞/重叠、采样率或布局变化尚未实现 `swresample async` 等价补偿，不能默默抹掉 PTS。

## 完整 AAC 的扩展验证

候选来自 [VerdaAI/torch-aac](https://github.com/VerdaAI/torch-aac/tree/5dfc83fa446292da0520185e3da24374fdfd9922)，精确源提交 `5dfc83fa446292da0520185e3da24374fdfd9922`。包为 Alpha、Elastic-2.0；本工程未把整个包源码复制入仓库。许可证与实际使用边界须按具体发布方式处理，不把其名称当作自动不能内部测试的理由。

`AACEncoder(sample_rate=48000, channels=2, bitrate=192000, device="cuda", batch_size=64)` 使用真实 CUDA，禁用隐式 `auto` CPU 降级。`DifferentiableAAC` 不输出 AAC 位流，未计入编码通过。

### 延迟、尾部及打包

* 原始 2 s/96000 样本编码解出 95232 样本，短尾 768；不能直接进入生产。
* 在 GPU 上首尾各补 1024 样本后，候选与 FFmpeg 原生 AAC 参考均解出 96256 样本（含 AAC 帧尾填充），前 96000 样本最佳偏移 0。4 s 对白均解出 192512 样本，最佳偏移也为 0。
* 仅靠 FFmpeg stream-copy `-t 4` 会把候选流时长写为 4.010 s，故不作为正确方案。改为 PyAV 离线资格验证 mux：AAC raw AU 的 PTS/DTS=`i*1024-1024`，尾包 duration=`min(1024, target_samples-pts)`，删除 `pts>=target_samples` 的全尾包。2 s 为95包、尾duration768；4 s为189包、尾duration512。所有 stream/format时长精确为2.000000/4.000000、start=0，解码PCM与前次补偿输出逐字节相同。记录在 `aac-exact-timeline-comparison.json`；最终 GPU MP4 mux 可直接采用这组包时序，但与最终视频/封装链仍须整体同步验收。
* 5 组随机码字（含空、零长、32 位、跨字节）GPU 打包与独立位串参考完全一致；真实 2 s 候选 96 次打包与原 C BitWriter 输出字节一致。
* GPU ADTS 单/双声道、采样率索引 0/3/12、payload 长度 1/37/8184 的 6 组合通过；95 帧真实候选 AAC 去头后在 GPU 重建，与原 AAC 全文件字节一致。该类是已验证独立封装算子，尚未替换候选全部逐帧调用。

### 真实对白及复杂信号

读取已存在的诊断成片 `baseline.mp4` 前 4 s，不改原文件；另生成 2 s 双声道 chirp、宽带噪声、包络和脉冲组合。全部与原生 FFmpeg AAC-LC/192k 同源对照；下表是未经自动时间平移的全波形 SNR。

| 对白编码路径 | 声明目标码率 | 实际 AAC payload 码率 | 全波形 SNR | 分段 SNR 中位数 / P10 |
| --- | ---: | ---: | ---: | ---: |
| FFmpeg 基线 | 192k | ffprobe 流码率179.639k | 44.46 dB | 48.33 / 37.08 dB |
| uniform，默认 | 192k | 211.80k | 28.97 dB | 33.31 / 20.78 dB |
| uniform，提高码率，仅诊断 | 256k | 276.12k | 33.40 dB | 38.29 / 25.05 dB |
| uniform + M/S | 192k | 174.36k | 35.20 dB | 42.69 / 28.77 dB |
| perband，修复 joint-gain 形状 | 192k | 216.29k | 21.99 dB | 24.80 / 13.66 dB |

原包 perband 真实对白含混合长/短窗时异常：`shape mismatch: value tensor of shape [63] cannot be broadcast to indexing result of shape [63, 2]`。隔离 adapter 在 GPU 把共同 gain 广播为两声道后严格解码通过，但质量没有改善。它是可撤销、版本限定的资格验证补丁，未写入安装包或生产路径。

提高码率没有被作为交付规格变更；实际码率按 ADTS 帧长和 1024 样本帧时长计算，payload 与 ADTS 总码率分别记录，不能将配置的 192k 当作实测码率。

复杂宽带夹具下，uniform192 全波形 SNR15.80 dB，高于 FFmpeg10.35 dB；perband 为8.11 dB，M/S 为14.55 dB。这表明单一 SNR 并非普遍听感排序（参考编码器的高频低通也影响全带宽误差）。脚本同时记录分段 SNR、对数频谱误差、峰值、样本数、时差与严格解码。真实音乐/主观听测、长期同步、流式 flush/分批连续编码和负载稳定性尚无通过证据，完整 AAC 不上线。

## 可迁移与仍在 CPU 的准确划分

| 环节 | 当前证明 | 剩余工作 |
| --- | --- | --- |
| PCM 整数归一化、混音/声道矩阵、重采样、静音和裁断 | 本工程 CUDA 算子通过连续 mono/stereo 夹具 | 多声道/异常时钟走下述显式 CPU DSP 兼容；完整制作链仍需验收 |
| AAC window/MDCT、量化、码本选择 | torch-aac 实际 CUDA 执行 | 候选音质与码率控制仍未达原路径资格 |
| 长窗且无 M/S 的 spectral Huffman | 包内 GPU gather | 其接口最后把码字下载为 NumPy；后续语法计算仍 CPU |
| 短窗/M/S spectral Huffman | 包内明确 CPU 路径 | 补 GPU 四元/二元码本、sign/escape、窗口组顺序；与全部合法码字逐比特比对 |
| block-switch | GPU 瞬态检测 + CPU Python 状态机 | 将状态转移做每流 GPU 顺序扫描，保留跨 batch 状态 |
| ICS/max_sfb/section/scalefactor 语法 | CPU Python/NumPy | 可一帧/声道一线程生成字段，再前缀和+打包；需要长短窗、共享窗口、M/S、PNS、escape 的完整语法测试；本轮未实现 |
| 码字 prefix-sum/bitpack | 本工程 CUDA，通过真实字节一致 | adapter 仍接收 CPU 生成的语法符号；不是全 GPU 编码 |
| ADTS 头、长度、位移和复制 | 本工程 CUDA，通过真实字节一致 | 尚未接入所有编码分支；CPU 目前仍有原包头生成 |
| AAC/其他压缩源解码 | 明确 CPU PyAV/FFmpeg | 没有已验证完整 GPU decoder；NVIDIA 视频硬件接口不等于音频 codec |
| 文件读取、host↔device 传送、写文件、调用控制 | CPU/OS | 此处不计成已迁 GPU，也不把它们混作音频数值计算 |

以上未迁项不应表述为硬件永远无法做。当前限制是成熟实现、集成完整性和输出质量的实测证据。调度/元数据控制仍由 Python 驱动，CUDA 字段/位流计算与 CPU 系统 I/O 分别标注。

## 集成接口与后续验收

### 已实现的渐进接口

`prepare_audio_track(source, output_dir, duration_seconds, check=None, timeline_origin_seconds=None, *, audio_bitrate=192000, ffmpeg="/usr/bin/ffmpeg")` 返回 `PreparedAudio(track, spool, report)`。码率是整数 bps，默认192000；X等原128000契约须传实际值，报告记录实参：

* `track` 为 `gpu_mux.Track("audio", 48000, samples, ASC)`；每个 sample 持有 `PacketFile(spool,offset,size)` 原始 AAC AU、编码器实际 PTS/DTS/duration，直接供 GPU MP4 mux 使用，不调用 CPU 成片封装器。
* `spool` 是唯一命名文件，成功后由调用方管理生命周期；失败/取消立即清理 spool 与临时 PCM。
* `report` 明确 `dsp_backend=cuda`、`decode_backend=cpu_pyav`、`encode_backend=cpu_native_aac`，记录 libavcodec 版本、目标192k、有效样本数、priming/末包duration等。
* 输入按容器起点归零，或者调用方显式提供共同音视频 origin。只解码至目标结束加 FIR halo（允许跨越一个解码帧），避免为短输出解码整个长源片。每解码帧、每 CUDA block、每编码 packet 调用 `check()`；该回调可抛出取消异常。
* fltp 的平面排列也在 GPU 完成，送 native AAC 的格式/声道/采样率均已匹配。保留 CPU 文件拷贝、原生压缩 codec 和调用控制，不把它们声称为 GPU 计算。

真实4 s对白与系统 `/usr/bin/ffmpeg` 参考：189包时间戳及duration完全相同；ASC为 `119056e500`、有效192000样本、priming1024、尾duration512；严格解码PCM SHA-256均为 `009609f451fa142525ade588af23a354a6fd28175e64504e7e8213b69fda393c`。只有第0压缩包的 `Lavc61.19.101` 与 `Lavc62.11.100` 标识不同，其余188包相同。系统 FFmpeg 已由主代理确认是当前生产参考。

`audio-track-expanded.json` 的12项集成检查均通过：

| 场景 | 解码PCM相对现有参考 | 独立未压缩参考PCM的 SNR：基线 / 候选 |
| --- | --- | --- |
| 48k stereo长音轨、48k mono短音轨补静音 | 逐样本相同 | 相同 |
| +0.25s音频起点、无音轨静音 | 逐样本相同 | 相同 |
| .01/.10001/1.00001s | 逐样本相同；解码1024/5120/48128样本 | 相同 |
| 44.1k mono纯音 | 不同，候选-参考37.99dB | 37.51 / 47.09dB |
| 16k mono | 不同，候选-参考54.03dB | 43.76 / 43.44dB |
| 96k stereo | 不同，候选-参考37.41dB | 33.70 / 33.86dB |
| 44.1k真实对白 | 候选-参考55.46dB，最大差0.001073 | 39.604 / 39.612dB |
| 编码前取消 | 临时PCM/spool全部清理 | 不适用 |

极短 .01s 有效480样本，保留首DTS=-1024的priming包，末packet.duration=480；独立解码器输出1024包含AAC填充，和参考相同。不得用 `seek(0)` 丢弃音频负PTS首包。

验证过程保留了最初 `audio-track-integration.json` 的失败记录：当时误把“两个有损输出的差异SNR≥50dB”当成质量门槛，44.1k纯音未达到。进一步对同一未压缩参考PCM分别评估两种输出，候选实际更高。更新后的数值回归判据为样本数/时序一致且候选对独立PCM的SNR不比基线低超过0.5dB，并继续保留候选-参考差异，不隐藏原失败。该数值判据不等于主观听测通过；完整视频+GPU mux结果仍由整链QA验收。

### 异常时钟/多声道的明确兼容

为避免原本支持的源文件在新路径新增失败，`prepare_audio_track` 在任何 CUDA 工作之前完成解码/时钟校验。仅下列 `AudioQualificationError` 原因选择现行 FFmpeg CPU DSP：

* `channel_layout_requires_explicit_matrix`：当前不具有已验证矩阵的多声道源。
* `discontinuous_audio_timestamps`、`missing_audio_timestamps`：时间戳空洞/重叠/缺失。
* `changing_audio_format`：中途采样率、布局或数据类型变化。

兼容分支使用原 `aresample=48000:async=1:first_pts=0,apad` 语义，输出到明确标识的临时 PCM，之后仍由 native AAC 编码出 AU 供 GPU MP4 mux。日志告警及 report 的 `dsp_backend=cpu_ffmpeg_compatibility`、`compatibility_reason`、`cpu_residual` 明确记录真实 CPU DSP。**CUDA 初始化、kernel、显存、编码、I/O 或未知异常不会进入该分支**；故障注入 `forced_cuda_failure` 验证不会静默降级，临时目录不残留。

兼容 FFmpeg 不新建进程 session，留在父 worker 进程组，供父 watchdog 统一终止。等待时只在 `compat.pcm` 实际文件字节数增长后调用推进 `check()`，不发送无进展的周期心跳；取消异常终止自己的 FFmpeg 子进程并回收临时文件。

192k的人工100ms PTS空洞与5.1声道样本均与原 FFmpeg 解码PCM逐样本相同，扩展为14项通过（`audio-track-compat-expanded-192k.json`）。128k包含这些兼容、全部原场景、取消与CUDA错误不降级共15项通过（`audio-track-compat-expanded-128k.json`）；128k真实44.1k对白对独立PCM的候选/基线SNR为29.153/29.343dB，差-0.189dB。

主代理在完整30s制作发现真实HEVC素材前6s连续、到30s出现PTS断点。对其只读前30s实测兼容后：1408个AAC包、priming1024、尾duration256、有效1440000样本；stream/format均30.000000s、start0。独立解码1440768样本（含AAC尾padding），与当前FFmpeg参考PCM逐样本完全相同，记录 `real-30s-compat.json`。该样本的DSP仍是CPU兼容，不能把完整成片成功写成此样本音频DSP已迁GPU。

上述已知兼容源的完整 GPU DSP 时间戳补偿、多声道矩阵仍未完成，不以“GPU无能力”解释，也不以关闭原支持范围代替迁移。

### 低层接口与尚需验收

`decode_contiguous_audio(source, pcm_path)` 明确在 CPU 解码，只保留原始 PCM 类型/声道和真实 PTS；`.array()` 是 memmap。调用方必须按视频/容器起点建立共同时间基准，不能把原始绝对音频 PTS 直接当成从 0 开始的输出时间。

`AudioTimeline.create(input_rate=..., input_samples=..., duration_seconds=..., start_seconds=...)` 与 `CudaAudioDsp.write_pcm(pcm, timeline, handle)` 输出 48k/stereo/f32le。单声道增益采用 FFmpeg 默认 center→L/R 的 sqrt(1/2)，避免无意增大音量。大文件使用分块与 halo，不全量占用显存。

渐进接口已经让 CUDA DSP 输出送原生 CPU AAC 编码器，但仍明确有 CPU 压缩解码和 AAC 编码；只有生产调用实际选择并完整文件通过后，才算生产接入。完整 GPU AAC 路径现阶段不满足上线条件。

用户明确要求：如果部署需要暂停制作任务，先告知用户，由用户暂停并确认后才能部署；本记录不授权任何代理暂停、drain、重启或切换生产。

## 原始依据

* [torch-aac encoder 源码](https://github.com/VerdaAI/torch-aac/blob/5dfc83fa446292da0520185e3da24374fdfd9922/torch_aac/encoder.py)：GPU/CPU 分支、形状、默认参数、状态机与 ADTS。
* [torch-aac bitstream 源码](https://github.com/VerdaAI/torch-aac/blob/5dfc83fa446292da0520185e3da24374fdfd9922/torch_aac/cpu/bitstream.py)：ICS、section、scalefactor 与位打包接口。
* [torch-aac GPU Huffman 源码](https://github.com/VerdaAI/torch-aac/blob/5dfc83fa446292da0520185e3da24374fdfd9922/torch_aac/gpu/huffman_encode.py)：长窗 pair codebooks GPU 查表及 CPU NumPy 返回边界。
* [FFmpeg n7.1 resample.c](https://github.com/FFmpeg/FFmpeg/blob/n7.1/libswresample/resample.c)：信号端点镜像语义；本工程实现独立 CUDA sinc/Blackman 计算，未照搬 FFmpeg 源码。
