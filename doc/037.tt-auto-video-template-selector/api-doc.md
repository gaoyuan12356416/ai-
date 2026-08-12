# API 合同

## 模板请求

`POST /api/admin/tt-auto-publish/templates` 与模板更新请求新增：

```json
{"video_template":"random_overlay"}
```

允许值：

- `random_overlay`：`tt-post-random-overlay-hevc-720x1280-v3`，trim `0`；
- `direct_outro`：`tt-post-direct-outro-hevc-720x1280-v2`，trim `4.333333`。

写请求规则：字段必填；省略时返回 `tt_auto_video_template_required`、HTTP 409，未知值返回 `invalid_request`、HTTP 400。

## 模板响应

新保存版本在 `template.config.video_template` 返回显式值。历史数据库版本不做回填；服务执行和
页面回填仍按 `random_overlay` 兼容。该历史读取兼容不适用于新的创建/更新请求。

## Health

保留现有字段，并增加：

```json
{
  "video_templates": [
    {"key":"random_overlay","profile":"tt-post-random-overlay-hevc-720x1280-v3","source_trim_tail_seconds":0.0},
    {"key":"direct_outro","profile":"tt-post-direct-outro-hevc-720x1280-v2","source_trim_tail_seconds":4.333333}
  ]
}
```

响应不得包含 GPU URL、内部 bearer、credential seal key 或其他秘密。

## GPU 合同

浏览器不能提交 profile、trim 或 GPU URL。CPU 根据冻结模板枚举选择服务端固定路由，仍发送
`expected_profile`；GPU 必须精确回显匹配 profile，否则 CPU 拒绝成片。
