# API 文档

## 接口列表
原型阶段无新增 API。

后续正式实现优先复用：
- `GET /api/ad-control/products`
- `GET /api/ad-control/accounts?product=...`
- `GET/POST /api/ad-control/account-groups`
- `GET/POST /api/ad-control/rule-sets`
- `GET/POST /api/ad-control/bindings`
- `POST /api/ad-control/bindings/{id}/preview-live`

## 请求/响应
无变更。

## 错误码
无变更。

## 兼容性说明
新前端必须兼容现有数据模型，不破坏旧页面缓存和旧 API。
