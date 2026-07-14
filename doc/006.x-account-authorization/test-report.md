# 测试报告

## 测试结论

本地测试通过，满足进入生产灰度部署条件；生产 API、浏览器和真实 X OAuth闭环待部署后回填。

## 测试范围

- OAuth state/PKCE、过期/重放、多账号 upsert和 Token文件隔离。
- Scope下限、Token过期/刷新/撤销、Refresh Token轮换和 Token属主。
- 双 verify及 callback-vs-verify并发。
- loopback/internal bearer、30x Authorization防泄漏、callback日志脱敏。
- AI 后台权限路由、错误白名单、页面/导航/Nginx/systemd配置。
- 主单体及规定回归模块编译。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| X功能自动化 | 16 | 16 | 0 | 0 |
| Python编译组 | 11 | 11 | 0 | 0 |
| JS/JSON/diff静态检查 | 4 | 4 | 0 | 0 |
| 两轮只读代码评审 | 2 | 2 | 0 | 0 |
| 生产/真实 OAuth | 1 | 0 | 0 | 1（待部署） |

## 缺陷情况

- BUG-001：callback日志与精确代理，已修复。
- BUG-002：Token刷新/重新授权并发一致性，已修复。
- BUG-003：Token属主与必需 scope校验，已修复。
- 最终复核未发现仍然确定的 P0/P1。

## 验证证据

```text
python scripts/test_x_accounts.py -> Ran 16 tests, OK
python -m py_compile ... -> exit 0
node --check static/quick-nav.js -> exit 0
node --check <x-accounts inline script> -> exit 0
ConvertFrom-Json static/navigation.json -> success
git diff --check -> exit 0
```

## 遗留风险

- 真实 X OAuth必须由用户在 X官方页面确认；本地测试使用 Mock X响应，不能代替真实授权。
- X API可用性/计费由平台控制；页面不自动轮询，只在主动校验时请求。

## 发布建议

允许按 GitHub精确提交部署。必须先校验 live hash、完整备份、服务器 Python 3.9编译/测试，再窄重启 sidecar和主 API并执行公网/浏览器验证。
