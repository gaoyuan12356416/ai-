# BUG-001: source_direct 误拒绝 HEVC 原片

## 现象

自动发布任务 34 选择素材 `6028067` 后，在 GPU prepare 阶段返回：

`prepared_media_invalid: prepared output does not match the TikTok media profile`

任务尚未调用 TikTok publish。

## 生产证据

对冻结的源素材做只读 ffprobe：

- 视频：HEVC Main、`hvc1`、720×1280、30fps、`yuv420p`
- 音频：AAC-LC、双声道、44.1kHz
- 时长：107.6 秒
- 平均码率：约 1.39Mbps，低于 1.9Mbps 上限

除视频编码外，其余字段均满足 `source_direct` 合同。现有正式 `direct_outro` 链路也使用经过验证的 HEVC/`hvc1` 输出，因此 HEVC 本身不是 Direct Post 禁用格式。

## 根因

`source_direct` 保留源字节，但首次 `validate_prepared_output()` 和 manifest 重放 `_prepare_response()` 都把视频合同硬编码为 H.264/`avc1`。代码虽已允许多个 H.264 profile，却没有允许 HEVC Main/`hvc1`。

## 修复

- 保持 `source_direct` 不执行 FFmpeg，源文件 SHA 和大小仍必须与输出完全一致。
- 允许两组严格配对的视频合同：
  - H.264 + `avc1` + Baseline/Constrained Baseline/Main/High
  - HEVC + `hvc1` + Main
- 首次 prepare 与 manifest 重放共用同一匹配函数。
- 返回实际探测到的 codec/tag，不再把 source_direct 响应固定写成 H.264/`avc1`。
- VP9、编码/tag 错配、HEVC Main 10、非 `yuv420p` 等继续 fail-closed。

## 回归边界

未修改 profile 名称、数据库、CPU 调度、TikTok publish、生产闸门、输出 origin、大小/时长/码率、分辨率/帧率、像素格式或音频合同。
