# API 文档

## 接口列表

| 方法 | 路径 | 鉴权 | 用途 |
| --- | --- | --- | --- |
| POST | `/api/admin/x-accounts/{id}/drama-language` | Cookie 管理员 + 同源 JSON | 更新账号剧语言 |
| POST | `/internal/accounts/{id}/drama-language` | loopback backend bearer | 后端到账号 sidecar 转发 |
| POST | `/internal/posts/premium-relay/accounts` | daily bearer | 按剧语言查询 Premium 中继 |

## 请求/响应

更新账号：

```json
{"drama_language":"ja"}
```

成功响应沿用账号 DTO，新增：

```json
{"item":{"id":19,"username":"example","drama_language":"ja"}}
```

Premium 中继请求：

```json
{"run_date":"2026-08-14","drama_language":"ja"}
```

账号查询、账号选项和 X Auto 账号接口返回的每个账号均新增 `drama_language`。

## 错误码

| 错误码 | HTTP | 说明 |
| --- | --- | --- |
| `x_account_drama_language_invalid` | 400 | 语言标签非法 |
| `x_admin_required` | 403 | 非管理员更新 |
| `x_account_not_found` | 404 | 账号不存在 |
| `x_account_drama_language_conflict` | 409 | 未完结绑定短剧冲突 |
| `x_post_account_language_mismatch` | 409 | 内容与账号语言不匹配 |
| `x_post_drama_account_language_mismatch` | 409 | 已绑定短剧与账号语言不匹配 |
| `x_auto_account_language_mismatch` | 409 | X Auto 模板/任务与账号语言不匹配 |

## 兼容性说明

- 旧账号通过 additive migration 获得默认 `en`。
- `jp` 输入规范为 `ja`；历史模板和源数据不回写。
- 旧历史队列新增语言列默认 `en`、冻结标记默认 0，不会因为状态更新被重新路由或被新规则误拦截；只有新调用显式携带账号语言时冻结标记为 1。
- Premium 中继接口旧调用未传语言时按 `en` 处理；新调度器始终显式传入语言。
