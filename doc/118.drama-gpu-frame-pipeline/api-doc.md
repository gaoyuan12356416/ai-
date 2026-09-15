# 接口和配置

无外部 API 变更，任务 ID、提交/恢复请求、结果 URL 和进度字段兼容。

| 配置 | 生产目标值 |
| --- | --- |
| DRAMA_GPU_FRAME_PIPELINE | cuda；默认 opencl |
| DRAMA_GPU_ASSET_CACHE_ROOT | /data/drama-synthesis-gpu/assets/rgba-nut-demux-v2 |
| DRAMA_GPU_RUNTIME_IDENTITY | nvdec-cuda-nvenc-rgba-demux-v2 |
| PYTHONPATH | /data/drama-synthesis-gpu/runtime/native-cu124-v1 |
| CUPY_CACHE_DIR | /data/drama-synthesis-gpu/work/cache/cupy-native-v1 |
| LD_LIBRARY_PATH | 现有 runtime 的 cuda_nvrtc/lib 与 cuda_runtime/lib |

`DRAMA_GPU_COMPOSITOR_BACKEND=opencl_fused_v2` 继续表示原有分片调度器；实际帧链路由 FRAME_PIPELINE 选择。健康接口增加 frame_pipeline / asset_cache_enabled；启动预检 cuda_tested=true 只在真实硬解、CUDA 合成、硬编码样片通过后返回。

可选依赖 PyNvVideoCodec 1.0.2、av 12.3.0、cupy-cuda12x 13.3.0、fastrlock 0.8.3 安装到独立目录，不替换系统 Python 或已有 torch。
