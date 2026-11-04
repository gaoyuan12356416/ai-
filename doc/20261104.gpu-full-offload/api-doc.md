# API 文档

## 接口列表

不新增外部API、不新增发布调用。FB prepare请求/结果、冻结source SHA/recipe、COS身份和回执合同不变。

- `native_pipeline.command(source, output, info, recipe, assets)`：构造独立制作子进程；spec仅包含本地路径、媒体probe、冻结recipe，不含URL或凭据。
- `encode_video(...)`：NVDEC→CUDA→NVENC，返回压缩AU位置及PTS/DTS/时长；不上传。
- `prepare_audio_track(...)`：返回AAC AU spool及实际ASC，供GPU mux。GPU DSP与CPU codec分别报告。
- `CudaMP4.mux(...)`：GPU索引/时序/偏移/箱体字节编码；主机文件IO。时间来自实际AU，负AAC priming通过edit list补偿。
- `file_digest()`：GPU节点显式开启后使用标准CUDA SHA-256，CPU backend保持原实现。

## 请求/响应

内部诊断 `<output>.native.json`记录实现版本、编码参数、各阶段时间与CPU残留，不进入外部发布payload。子进程的`.progress`只由实际解码/计算/写包推进，父进程负责超时、kill+wait及scratch清理。

## 错误码

外部沿用 `fb_gpu_transcode_failed`；内部有具体video/audio/mux验证异常，runner保留最多8192字节错误尾部用于排障。GPU错误不会伪造成功，也不会主动发布/重试未知结果。

## 兼容性说明

GPU文件SHA保持普通SHA-256字节级语义；不使用分块树哈希。小型控制/身份哈希、JSON、网络认证与调度仍为主机控制工作。

目前native制作接入FB固定H264/CQ21合同；X码率限制、TT P6/HEVC配置不能无验证套用。完整AAC候选未达到真实对白质量，保留原CPU AAC；PCM重采样/声道处理可迁项使用GPU。已知特殊音频时钟/布局按明确兼容原因处理，不将CUDA运行错误静默降级。
