# API 文档

## 接口列表

沿用现有 `/api/ad-control/v3/*`。重点影响：`/meta`、`/rule-groups`、`/executions`、`/executions/{id}`、Preview/Execute 响应。

## 请求/响应

- 请求结构不变。
- 所有 V3 JSON 响应增加 `X-Ad-Control-Timezone: UTC+8`。
- `/meta.time_standard`：

```json
{"storage_timezone":"UTC","display_timezone":"UTC+8","iana_timezone":"Asia/Shanghai"}
```

- 已知审计字段从 UTC 无偏移文本转换为带 `+08:00` 的 ISO 8601，例如：

```text
存储：2026-07-17 06:55:00.000000
响应：2026-07-17T14:55:00.000000+08:00
```

- `date_from/date_to` 仍传 `YYYY-MM-DD`，但含义固定为 UTC+8 自然日。

## 错误码

- `copy_name_update_failed`：Meta 名称更新失败或响应异常。
- `copy_name_readback_failed`：名称或 PAUSED 状态回读不一致。
- `copy_mapping_incomplete`：来源关系、父级关系或 Creative 映射回读不一致。
- 其他 copy 隔离、幂等和 Meta 错误码保持不变。

## 兼容性说明

数据库仍保存 UTC；客户端若已直接消费 V3 时间字符串，应支持标准 ISO 8601 `+08:00`。Meta `start_time` 和广告账号本地调度不属于审计时间转换范围。
