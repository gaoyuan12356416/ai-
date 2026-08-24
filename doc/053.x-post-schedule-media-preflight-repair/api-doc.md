# API 说明

## 对外接口

无变化。

## 内部 schedule-plan candidate

material schedule 新队列恢复完整预检合同：

- `media_validation_mode=preflight`
- `preflight_sha256` 为 64 位小写 SHA256
- `preflight_size > 0`
- `preflight_duration/preflight_width/preflight_height` 来自最终 CPU probe
- 发生重制时携带 existing repair audit fields

drama schedule 继续使用 `media_validation_mode=deferred`，接口合同不变。

## 兼容性

历史 preflight/deferred 队列均按 frozen state 执行，不迁移、不重写。
