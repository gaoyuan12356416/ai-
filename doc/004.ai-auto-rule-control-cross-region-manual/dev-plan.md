# 开发计划

## 开发范围
对已上线的 `AI自动规则调控` 做系统测试和文档固化，覆盖前端拆页、跨区手动配置、后端规则字段、导航一致性和线上只读安全验证。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求与边界整理 | Codex | `doc/004.../requirements.md` | 完成 |
| 本地静态校验 | Codex | `app.py`、`static/*.js` | 完成 |
| 后端冒烟测试 | Codex | `app.py` ad-control helper | 完成 |
| 线上只读校验 | Codex | ai.yingliangads.com | 完成 |
| 浏览器 DOM 校验 | Codex | 7 个 ad-control 页面 | 完成 |
| 测试报告归档 | Codex | `doc/004.../test-report.md` | 完成 |

## 编译 / 构建命令

```bash
python -m py_compile app.py
node --check static/quick-nav.js
node --check static/ad-control-pages.js
git diff --check
```

## 风险与依赖
- 依赖线上登录态与真实 Meta token 的 CRUD/preview/execute 未在本轮执行。
- 真实关闭必须继续保留 preview hash + 确认口令。
- 后续自动 runner 开启前，需要补“当天禁止重启、隔天允许重启”的服务端执行测试。

## 完成记录
- 2026-06-30 完成本地编译、静态资源、规则匹配、线上页面、导航、状态库和服务日志系统测试。
