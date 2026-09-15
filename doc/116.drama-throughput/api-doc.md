# 接口兼容性

无新增公开接口、表或必填字段。

GPU 任务查询增加 stage：prefetching、prefetched；status 均为 queued。progress 沿用 downloaded_bytes、total_bytes、completed_episodes、total_episodes、bytes_per_second、download_workers。私有输入与内部预取标记不进入 DTO。

- DRAMA_JOB_WORKER_OBSERVERS：1/2，默认 1。
- DRAMA_GPU_PREFETCH_ENABLED：仅 1 启用。
- DRAMA_GPU_PREFETCH_DURING_DOWNLOAD：仅 1 允许与当前任务下载重叠，默认关闭。启用预取时总下载并发不得超过 8。
- DRAMA_GPU_PREFETCH_WORKERS：1/2/4，默认 2。
- DRAMA_GPU_PREFETCH_MAX_BYTES：默认 16 GiB。
- DRAMA_GPU_PREFETCH_MIN_FREE_BYTES：默认 20 GiB。
- 正式下载仍使用 DRAMA_GPU_DOWNLOAD_WORKERS，与 GPU 合成并发独立。
