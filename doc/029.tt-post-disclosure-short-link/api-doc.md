# API 文档

API 请求/响应字段不变。

## 新自动任务合同

```json
{
  "id": 6,
  "short_link_id": 6,
  "short_url": "https://gy.g2flow.com/s2l/tt/6.html",
  "brand_content_toggle": false,
  "brand_organic_toggle": false,
  "commercial_disclosure": false
}
```

## 兼容性

- 历史自动链接 `/s2l/8xxxxxxxxxxxxxxxxxx.html` 保持有效。
- direct-test 继续使用其既有独立链接合同。
- X `/s2l/{id}.html` 保持原路由与文件。
- 无数据库 schema 变更。
