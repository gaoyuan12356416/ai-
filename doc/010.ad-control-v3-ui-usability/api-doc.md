# API 文档

## 接口列表

- `GET /api/ad-control/v3/meta`：产品、优化师、时区、层级与能力。
- `POST /api/ad-control/v3/scope-estimate`：按产品/优化师/层级估算结构。
- `POST|PUT /api/ad-control/v3/rule-groups[/<id>]`：保存规则组。
- `POST /api/ad-control/v3/rule-groups/<id>/preview`：手动试算。

## 请求/响应

公开请求结构不变。具体投放产品仍通过 `products: ["w2a:1723"]` 提交；客户端不能提交目录证据或内部范围。

`/meta.products[]` 示例：

```json
{
  "product_value": "w2a:1723",
  "canonical_product": "Dramawave",
  "product_type": "short_drama",
  "source_app_ids": [2477],
  "evidence": {
    "catalog_kind": "delivery_product",
    "display_name": "drama-double · W2A 1723",
    "platform_app_id": "1031273318485141",
    "scope": {
      "insight_products": ["Dramawave"],
      "insight_app_ids": ["[w2a]drama-double"],
      "w2a_page_ids": [1723]
    }
  }
}
```

`scope-estimate` 只回显公开 scope，不返回内部 `delivery_product_scopes`。

## 错误码

| code | HTTP | 含义 |
| --- | --- | --- |
| `invalid_product_scope` | 400 | 产品未启用或不存在 |
| `product_catalog_invalid` | 503 | 具体产品证据漂移/不完整，失败关闭 |
| `overlapping_product_scope` | 400 | 同时选择了重叠宽/细产品 |
| `ambiguous_product_scope` | target status | 对象跨多个具体产品，不执行动作 |
| `scope_query_deadline_exceeded` | 503 | 受限源查询超过总软截止时间 |

## 兼容性说明

- 原 15 个 product enum 和已有规则请求保持不变。
- `evidence` 是扩展字段，旧客户端可忽略。
- TT 不启用；V2 API 不变。
