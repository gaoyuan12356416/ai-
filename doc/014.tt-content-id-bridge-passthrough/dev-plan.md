# 开发计划

## 开发范围

新增独立公开页、参数拼接模块、契约测试和发布文档；不修改主服务路由。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 移动端页面 | Codex | `static/tt-drama-search.html` | 已完成 |
| 安全透传逻辑 | Codex | `static/tt-drama-search.js` | 已完成 |
| `/tt` 短路径 | Codex | `deploy/nginx/tt-drama-search.conf` | 已完成 |
| 契约测试 | Codex | `scripts/test_tt_drama_bridge.js` | 已通过 |
| 生产发布与浏览器验证 | Codex | AI CPU 静态目录 | 待执行 |
| 文档闭环 | Codex | `doc/014.*` | 进行中 |

## 编译 / 构建命令

```bash
node --check static/tt-drama-search.js
node scripts/test_tt_drama_bridge.js
```

## 风险与依赖

- 依赖生产 Nginx 的 `/usr/share/nginx/html` 静态根目录。
- 新增精确 Nginx location，发布时必须先执行 `nginx -t`，仅在通过后 reload。
- W2A 页面是否识别附加参数属于外部服务行为；本需求只验证最终链接的参数结构。
- 发布前同时备份应用静态副本和 Nginx 公开副本。

## 完成记录

- 2026-07-24：本地 Node 契约测试通过 31 项断言。
- 2026-07-24：390×844 Playwright 真实浏览器验证通过，生成示例链接与需求完全一致。
- 2026-07-24：从本地中间页实际进入 W2A，最终 URL 保留 `af_adset_id=XXX`，页面解析出对应日语剧集。
