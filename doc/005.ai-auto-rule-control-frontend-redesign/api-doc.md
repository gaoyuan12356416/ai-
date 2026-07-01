# API 文档

## 接口列表
原型阶段无新增 API。

后续正式实现优先复用：
- `GET /api/ad-control/products?products=dramawave,hotdrama,freereels`
- `GET /api/ad-control/accounts?product=...`
- `GET/POST /api/ad-control/account-groups`
- `GET/POST /api/ad-control/rule-sets`
- `GET/POST /api/ad-control/bindings`
- `POST /api/ad-control/bindings/{id}/preview-live`

## 请求/响应
计划变更：
- 产品列表增加产品值过滤参数，默认仅返回 `dramawave`、`hotdrama`、`freereels` 三个产品。
- 账号列表继续按 `product` 查询；前端多产品选择时分产品并发加载账号，合并展示但保留产品归属。

## 错误码
无变更。

## 兼容性说明
新前端必须兼容现有数据模型，不破坏旧页面缓存和旧 API。若后端暂未支持 `products` 参数，前端可以先基于返回字段做保守过滤，但最终以后端过滤为准。
