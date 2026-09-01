# API 文档

## 接口列表

- `GET /healthz`：沿用现有 GPU 健康接口，增加 V2 非敏感能力字段。
- `POST /api/gpu-video/jobs`：请求不变；随机模板由服务器配置选择 renderer。
- `GET /api/gpu-video/jobs/{job_id}`：状态不变，进度使用整段 timeline 指标。

## 请求/响应

业务请求与现有版本兼容。完成结果保留 `output_random_template_url`、`random_template_output_sha256`、`random_template_output_profile` 和 `random_template_recipe_sha256`。GPU 内部结果可额外包含 `renderer_profile`、`composition_sha256` 和 `chunk_count`，CPU 旧客户端可忽略。

## 错误码

| code | 含义 |
| --- | --- |
| `drama_gpu_compositor_unavailable` | 配置的 GPU shader backend 不可用 |
| `drama_composition_invalid` | Composition Spec 或变换超出合同 |
| `drama_render_chunk_failed` | 某视频分片制作失败，已完成分片保留 |
| `drama_render_chunk_timeout` | 某视频分片无进展或超过分片时限 |
| `drama_render_join_failed` | 已验证分片无法无重编码合并 |
| `drama_render_audio_mux_failed` | 最终连续音频封装失败 |

## 兼容性说明

未配置 `DRAMA_GPU_COMPOSITOR_BACKEND` 时保留 legacy 行为；生产启用 `opencl_fused_v2` 后不允许自动回退。业务 output profile 不变，renderer profile 独立版本化。
