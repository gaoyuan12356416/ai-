# API 文档

## 接口列表

不新增外部API、不新增发布调用。FB/X/TT制作请求、结果、冻结source SHA/recipe、COS身份、租约和回执合同不变。

- `native_pipeline.command(source, output, info, recipe, assets, *, cache_root=None, encoder_options=None, audio_bitrate=192000, ffmpeg="/usr/bin/ffmpeg")`：构造独立制作子进程；spec包含本地路径、媒体probe、冻结recipe和平台编码参数，不含URL或凭据。`info`保留视频time_base和容器start_time，duration为实际裁剪目标。
- `encode_video(..., encoder_options=...)`：NVDEC或事前选择的CPU源解码→CUDA→NVENC，返回H264/HEVC压缩AU位置及PTS/DTS/时长；不上传。CPU解码兼容不包含CPU缩放/图层合成。
- `prepare_audio_track(source, output_dir, duration_seconds, check=None, timeline_origin_seconds=None, *, audio_bitrate=192000, ffmpeg="/usr/bin/ffmpeg")`：返回 `PreparedAudio(track, spool, report)`，含原始AAC AU的PacketFile引用及实际ASC，直接供GPU mux。码率以整数bps传入；FB192000、X/TT128000，报告记录实际参数。
- `CudaMP4.mux(...)`：连续H264或单层HEVC+可选AAC的GPU交织/索引/时序/偏移/箱体字节编码，生成avc1/hvc1；主机计划轨道/box结构并执行文件IO及原子提交。时间来自实际AU，负AAC priming通过edit list补偿；不是任意MP4格式的通用替代。
- `file_digest()`：GPU节点显式开启后使用标准CUDA SHA-256，CPU backend保持原实现。
- `OutputColorContract(nvc, directory)`：HEVC使用的固定输出色彩上下文。将内嵌生成黑片写入scratch，通过SDK官方AVFormatContext传递limited BT.601/30fps，生命周期覆盖encoder释放；不读取真实源的颜色作为输出合同，不使用ctypes修改结构。

## 请求/响应

内部诊断 `<output>.native.json`记录实现版本、编码参数、各阶段时间与CPU残留，不进入外部发布payload。视频记录固定色彩合同标识，不记录临时AVFormatContext指针。音频report分别记录 `decode_backend`、`dsp_backend`、`encode_backend`、`compatibility_reason`、目标码率、有效样本数、priming及尾包duration。

音频spool成功后交由调用者管理，取消/失败清理临时PCM与spool。子进程的`.progress`只由实际解码/计算/写包推进；CPU音频兼容子进程仅在PCM文件字节增长时报告推进，不用定时心跳掩盖停滞。父进程负责超时、进程组kill+wait及scratch清理。TT始终经过self.runner和executor度量包装，native命令由media_runner转入共有run_render。

## 错误码

外部沿用各平台现有制作失败合同（FB为 `fb_gpu_transcode_failed`）；内部有具体video/audio/mux验证异常，runner保留最多8192字节错误尾部用于排障。GPU错误不会伪造成功，也不会主动发布/重试未知结果。

## 兼容性说明

GPU文件SHA保持普通SHA-256字节级语义；不使用分块树哈希。小型控制/身份哈希、JSON、网络认证与调度仍为主机控制工作。

FB、X、TT候选均已接入并分别验证原合同。X保留动态码率/maxrate/VBV与大小验收；SDK同时传CQ/预算会清空预算的问题已用正式Reconfigure修复，getter构造快照与重配请求分别报告。TT保留HEVC900k/1350k/1800k VBV或H2641500k/2200k/3000k VBV，均为P6/HQ/VBR/fullres/lookahead32/AQ8/temporal AQ/B3/middle。HEVC默认ABGR转换实际为BT.709，固定官方context修正为原TT的BT.601及正确VUI；H264原已验证默认不变。

音频连续mono/stereo走CPU压缩解码→CUDA归一化/声道/重采样/补静音/裁剪→CPU native AAC。下列已知输入错误在CUDA执行前选择原FFmpeg DSP：`channel_layout_requires_explicit_matrix`、`changing_audio_format`、`missing_audio_timestamps`、`discontinuous_audio_timestamps`；记录原因和CPU残留。CUDA初始化/运行、编码、IO及未知异常不能触发这一兼容支路。AAC解码输出可包含1024样本帧的尾padding，必须与容器有效样本数分别报告。

完整GPU AAC候选在合理参数/码率/延迟适配后仍未达到真实对白质量，且仍有CPU语法/部分Huffman工作；GPU bitpack/ADTS仅是隔离验证组件，不是生产完整AAC编码器。没有合格完整GPU压缩音频解码器，当前保留CPU codec是实现和质量边界，不是宣称GPU没有计算能力。

主机仍负责压缩包解封装、SDK/时间戳/小型参数控制、未合格格式的源视频解码、OS文件与网络IO。当前GDS通路未具备，不能把主机兼容传输称作存储直达GPU；具体环境依据见capability-review。上线状态与最终SHA验收见test-report/deploy，以上接口接入不等于已部署。按用户最新安排，本轮开发完成后停止，部署明天再进行，本轮不stage或切换生产。
