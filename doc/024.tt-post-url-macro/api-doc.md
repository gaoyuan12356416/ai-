# API 文档

## 接口列表

沿用现有 `/api/admin/tt-posts/material-pool`、`/run-now`、`/queue` 和后台发布 worker 接口，不新增公网接口。

## 请求/响应

请求模板示例：

```json
{
  "caption_template": "Watch now\n\nDrama ID: {{content_id}}\n\n{url}"
}
```

队列冻结结果示例：

```json
{
  "caption_text": "Watch now\n\nDrama ID: ABCD1234\n\nhttps://gy.g2flow.com/s2l/8000000000000000009.html",
  "short_link_id": 8000000000000000009,
  "short_url": "https://gy.g2flow.com/s2l/8000000000000000009.html"
}
```

跳转目标固定以 `https://www.dramawavew2a.com/ads/101/2250/view` 为基址，查询字段依次为：

1. `c=yingliang_post_CLV_VL_<username>*<timestamp>none<language>*<drama_name>*<tag>*<short_link_id>`
2. `af_adset=<account name>`
3. `af_adset_id=<account id>`
4. `af_ad=<material name>_contentid[<content_id>]`
5. `af_ad_id=<material id>`
6. `af_channel=AIpost`
7. `af_c_id=<TT queue id>`
8. `af_dp=<content_id>`

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `caption_placeholder_invalid` | 模板含未知或不完整宏 |
| `caption_url_required` | `{url}` 未绑定合法 TT 短链 |
| `tt_post_link_metadata_incomplete` | 归因元数据不完整 |
| `tt_short_link_target_invalid` | W2A 基址或参数非法 |
| `tt_short_link_conflict` | 同一短链编号已有不同目标 |
| `tt_short_link_write_failed` | 跳转页目录或写入失败 |

## 兼容性说明

- `{{contect_id}}` 和 `{{content_id}}` 继续兼容。
- `{url}` 可选；历史模板不会触发短链准备。
- API 仍传递普通 JSON 字符串，换行编码为 JSON `\n`，解析后保持真实换行。
