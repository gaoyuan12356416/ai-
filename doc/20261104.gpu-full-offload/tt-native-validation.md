# TT 随机模板 native 接入验证

## 运行合同与路由

2026-11-04只读香港运行进程及其node.env的限定非秘密键，确认 `TT_POST_GPU_MEDIA_MODE=random_overlay`、`TT_POST_GPU_VIDEO_ENCODER=hevc_nvenc`，FFmpeg/ffprobe为 `/data/pipeline-dispatch/tools/tt/ffmpeg` 和同目录 `ffprobe`。未设置显式 `TT_POST_GPU_PROFILE`，由既有配置选择HEVC随机模板profile。未打印全部环境或凭证。

当前默认HEVC不得改成H264。worker仅对随机模板、冻结的 `h264_nvenc/hevc_nvenc`、GPU compositor且 `RANDOM_GPU_NATIVE=1` 选择native命令；codec原值、trim后时长、配方、素材及128k音频保留。CPU/libx264、source-direct、normalize和outro路径不改原合同。

`inspect_input`新增保留视频time_base及容器start_time。`_run_command`仍调用 `self.runner`；executor外层原metrics包装仍标记render/probe。TT的`media_runner`只把native命令交给共有`run_render`，沿用真实进度watchdog、进程组终止与父进程scratch清理；普通探测及原FFmpeg调用继续subprocess.run。不在worker中绕过self.runner直接执行渲染。

## SDK参数依据与实测

依据NVIDIA PyNvVideoCodec2.2.3源码 `src/PyNvVideoCodec/src/NvEncoderClInterface.cpp`，不是按FFmpeg参数名猜映射：

| 合同 | SDK设置 / 实测 |
| --- | --- |
| P6/HQ | `preset=P6,tuning_info=high_quality`；TRACE读回一致 |
| VBR | `rc=vbr`；readback rateControlMode=1 |
| H264 1500k/2200k/3000k VBV | bitrate1500000/maxbitrate2200000/vbvbufsize3000000；全部readback一致 |
| HEVC 900k/1350k/1800k VBV | bitrate900000/maxbitrate1350000/vbvbufsize1800000；全部readback一致 |
| fullres | `multipass=fullres`；readback multiPass=2 |
| lookahead32 | `lookahead=32`；TRACE enableLookahead1/lookaheadDepth32 |
| spatial AQ强度8 | **`aq=8`**，不是aq=1；TRACE enableAQ1/aqStrength8 |
| temporal AQ | `temporalaq=1`；TRACE enableTemporalAQ1。源码按非空参数启用，不应用temporalaq=0假装关闭 |
| bf3 | `bf=3`；源码frameIntervalP=bf+1，readback GetNumBFrames3/TRACE frameIntervalP4；官方旧表IBBP描述不能替代实测 |
| middle B reference | SDK无b_ref_mode setter，当前T4/驱动/P6 preset实际useBFramesAsRef=2，TRACE断言验证 |
| GOP/闭合IDR | gop60/idrperiod60、fps30 |
| MP4 global header | global_header1，避免重复头改变VBR每帧预算 |
| Profile | SDK走Auto；实际独立ffprobe输出H264 High/HEVC Main，不传无效profile参数来伪装接受 |

vbvInitialDelay实测0，未擅自添加未证实的75%占用；colorspace参数会改变SDK RGB range行为，未传该参数，沿用已验证的native颜色合同。

官方参考：[PyNvVideoCodec编程指南](https://docs.nvidia.com/video-technologies/pynvvideocodec/pynvc-api-prog-guide/using_pynvvideocodec_apis.html)、[API参考](https://docs.nvidia.com/video-technologies/pynvvideocodec/pynvc-api-reference/encoder.html)。关键细节以2.2.3源码及实际T4配置读回交叉确认。

## 已执行验证

* `python -m unittest scripts.test_tt_native_adapter scripts.test_tt_gpu_worker`：96项通过（5新、91原）。覆盖双codec合同、audio128k、trim/原recipe不变、CPU不切native、self.runner观测及探测stdout保持。
* `scripts/verify_tt_native_encoder.py`：每codec仅一帧720×1280，在实际T4上初始化/编码；可读回rate/max/VBV/fps/rateControlMode/multiPass/B-frame值全部断言，TRACE同时断言AQ/lookahead/P6/middle。使用TT实际FFmpeg路径严格独立解码，High/Main、yuv420p、尺寸均正确。
* 实测证据：`/data/pipeline-dispatch/evaluations/gpu-full-offload-20261104/tt-encoder-qualification/result.json`；同目录H264/HEVC配置日志、原始码流。
* PyNvVideoCodec2.2.3在独立 `nvc223-experiment` target；基础音频venv仍装1.0.2。验收必须让2.2.3 target在PYTHONPATH前面。首次误用1.0.2的初始化不计验收，正式脚本主动核验版本2.2.3。

* 扩充执行 `python -m unittest scripts.test_tt_native_adapter scripts.test_tt_gpu_worker scripts.test_pipeline_executors`：119项通过；编译与diff检查通过。原self.runner metrics包装继续覆盖native render。

## 同源配方短链结果

`scripts/verify_tt_native_pipeline.py` 使用冻结的 `real-fixture/source.mp4` 与 `recipe.json`，两端均为TT真实FFmpeg、同一720×1280随机模板、同一码率合同，分别验证HEVC与H264各2秒/60帧。基线为native关闭且原GPU fuse开启，候选为native开启。该输入为实际HEVC长视频的前2秒，音频为44.1kHz stereo AAC。没有读取或变更发布请求。

| 指标 | HEVC | H264 |
| --- | --- | --- |
| 独立严格解码 | 通过 | 通过 |
| profile/tag | 两端Main/hvc1 | 两端High/avc1 |
| 帧数、时长、起点 | 两端60/2.000000s/0 | 两端60/2.000000s/0 |
| 逐帧展示时间 | 相同 | 相同 |
| 实际video bitrate（旧/新） | 1,138,644/1,134,956 bit/s | 1,893,372/1,907,116 bit/s |
| 候选与压缩基线PSNR/SSIM | 37.0533dB/0.970220 | 38.3725dB/0.982223 |
| 色彩标签 | 旧tv/bt470bg，新tv但matrix缺失；需修复 | 两端tv/bt470bg |

短链中的time_base由旧1/15360变为新1/30，但实际逐帧时间完全一致。两次验证的冷启动耗时不作为吞吐改善证据。PSNR/SSIM是与旧压缩成片的差异指标，不能等同于对无损素材的绝对质量或主观评价。

AAC保持LC/128k/48k/stereo，两端95包、有效96000样本、解码96256样本（包含尾部AAC padding），流与容器均2.000000秒。44.1→48k的GPU重采样与原CPU重采样不是逐比特相同；两端native AAC库版本分别61.19.101和62.11.100。`scripts/verify_tt_audio_comparison.py` 再对独立源PCM做全波形与首尾检查，基线SNR21.1160dB、候选21.0200dB，差−0.0960dB，在本次预先采用的−0.5dB数值回归界内；全片相关延迟均0样本，首尾100ms误差也已记录。不能宣称PCM完全一致或已经证明所有素材主观音质。

证据位于隔离根下 `tt-pipeline-hevc-2s/result.json`、`audio-quality.json`，以及 `tt-pipeline-h264-2s/result.json`。GPU fuse实际使用 `fps=30:start_time=0` 的原时钟，与native同容器origin；未用CPU旧setpts路径冒充GPU基线。

以上表格保留首次HEVC颜色问题证据；随后验证表明，正确修复必须同时更改错误的默认转换矩阵，不能只要求旧native像素不变。

## HEVC官方色彩上下文修复验证

新增 `color_contract.py` 原型通过SDK2.2.3官方 `source_av_format_context` 接口提供固定输出合同，未使用ctypes改内部结构。上下文来自内嵌1474字节、64×64、1帧、30fps的生成黑片，不含用户素材；参考SPS为limited BT.601，primaries/transfer unspecified，SHA256 `b5cb72c9a5b006b8df99621d8dd816091466f267cdb64a93a761bd6bbd2b930a`。仅把静态数据写入任务scratch并用SDK自身CreateDemuxer/GetAVFormatInputContext持有到encoder释放，生产无需再执行FFmpeg生成参考。

源码交叉核查：该参数读取色彩、帧率，并在global_header未明确时推导头部模式。**帧率读取优先于显式fps**，所以固定参考必须30/1并运行时检查；global_header1、gop60/idrperiod60均显式保留。不能使用实际源的色彩元数据，因为最终ABGR输出已经按固定目标渲染，源可能BT.709/HDR/full-range。

首次以“修复前后native YUV逐比特相同”为判据失败，保留报告 `hevc-color-context-60frames/result.json`。进一步色块测试定位默认HEVC编码ABGR实际按BT.709 limited转换，而旧TT FFmpeg按BT.601 limited转换；H264的默认行为不能直接套到HEVC。

| 纯色 Y/U/V均值 | 原native HEVC | 官方固定context | 原TT FFmpeg |
| --- | --- | --- | --- |
| 红 | 63/102/240 | 81/90/240 | 81/90/240 |
| 绿 | 173/42/26 | 145/54/34 | 145/53/34 |
| 蓝 | 32/240/118 | 41/240/110 | 41/240/110 |
| 黑 | 16/128/128 | 16/128/128 | 16/128/128 |
| 白 | 235/128/128 | 235/128/128 | 235/128/128 |

另含青/品红/黄三色，固定context相对旧FFmpeg全部色块误差最多1个8-bit级。60帧测试的TT P6/HQ、实际码率/VBV、fullres、AQ、lookahead32、B3/middle、GOP/IDR、global header以及包PTS读回一致。参考证据：`hevc-color-bars-60frames/result.json`（保留旧判据失败）与以旧FFmpeg作为正确oracle的 `hevc-color-bars-contract-final/result.json`。

`verify_hevc_color_scene.py` 用同一冻结TT真实2秒spec做独立完整渲染，通过官方context原型注入，不修改生产模块。修正后相对旧TT基线PSNR **38.8040dB**（修正前37.0533）、SSIM **0.972120**（修正前0.970220），严格解码通过，60帧展示时间一致，音频PCM与修正前候选完全相同，输出tv/bt470bg标签正确。证据：`tt-pipeline-hevc-color-2s/result.json`。该结果说明应修正矩阵及标签，不能为了与错误native输出保持字节一致而拒绝修复；最终native模块接入和服务用户环境验收由主任务完成。

无生产任务暂停、drain、服务重启、发布或部署。
