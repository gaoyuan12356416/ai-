# API 文档

## 接口列表

本需求不新增公网接口。复用以下内部接口：

- `POST /internal/posts/material-pool/available`
- `POST /internal/posts/material-pool/check`
- `POST /internal/posts/premium-relay/accounts`
- `GET http://127.0.0.1:8820/health`
- `POST http://127.0.0.1:8820/internal/x-post-media-repair`

## 请求/响应

内部路由和响应结构不变。媒体回填工具仍以重复 `--material-id` 指定显式范围；增加可选 `--force-repair`，对每个显式视频都重新执行 GPU 转码、COS 上传/HEAD 和 CPU 二次探测。报告增加 `force_repair` 布尔值，并返回 `validated_ready`、`repaired_ready` 或逐条安全错误码。

强制重制请求使用 `trigger_code=operator_forced_repair` 和独立确定性 job key。该原因只允许由显式 ID 回填产生，自动调度不生成。

## 错误码

- `material_language_not_scheduled`：仅表示当前配置确实没有该语言目标账号。
- `x_long_video_requires_premium`：仅表示当前没有可用的同语言 Premium relay。
- `repaired_media_invalid` / `cos_upload_failed`：由显式回填重新探测或修复。
- `operator_forced_repair`：运维显式要求重新制作，不代表源素材本身不合规。
- `material_source_tag_unsafe` / `material_has_violation`：保留历史展示兼容，但新代码不再产生或用于过滤。

## 兼容性说明

既有公网请求、SQLite schema、队列字段及 Premium relay 合约全部兼容；GPU 内部请求 allowlist 为向后兼容扩展，默认回填和日常调度行为保持不变。
