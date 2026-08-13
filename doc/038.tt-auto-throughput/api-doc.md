# API 文档

## CPU 内部执行接口

`POST /internal/tt-auto-post/execute-next`

```json
{"worker_id":"tt-auto-post-runner-primary-publish","phases":["publish","reconcile"]}
```

- `phases` 可省略，省略时保持旧的全阶段行为。
- 允许值：`selection`、`prepare`、`publish`、`reconcile`。
- 空数组、字符串或未知值返回 `400 invalid_request`。

## GPU prepare 响应扩展

```json
{"stage_timings_ms":{"asset_snapshot":2,"download":1000,"source_probe":40,"gpu_queue_wait":500,"transcode":50000,"output_verify":100,"upload":300,"total":51942}}
```

字段为非负整数毫秒；旧 manifest 可以不包含该对象。

## health 扩展

TT 自动发布 health 增加 `prepare_ahead_seconds`，不含敏感配置。
