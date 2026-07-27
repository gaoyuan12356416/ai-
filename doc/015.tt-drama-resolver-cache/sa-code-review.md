# SA 代码评审

## 结论

通过。两轮只读代码审查发现的精确匹配、连接回收、无界排队、Nginx
代理和旧 WebView 超时问题均已修复；当前无部署阻断项。

## 评审范围

- `features/tt_drama_resolver/` 的 SQL、只读连接、连接池、缓存、single-flight 和限流。
- `app.py` 的无鉴权精确路由、状态码、响应头、真实 IP 和全局并发闸门。
- `/tt` 的输入校验、异步竞态、封面回退、CTA 失败关闭和参数透传。
- Nginx 精确代理、CSP 与封面 CDN 预连接。
- 自动测试、线上只读 SQL 执行计划和 390x844 浏览器行为。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | 高 | `service.py` SQL | `_ci` 排序规则会把错误大小写 ID 当作命中 | 保留索引条件并增加 `BINARY` 精确条件，返回后再校验 canonical ID | 已修复 |
| CR-002 | 高 | `app.py` 公共路由 | 无界 HTTP 线程可因大量不同 ID 长时间排队 | 增加 32 在途全局闸门，满载立即 503 | 已修复 |
| CR-003 | 中 | `service.py` 连接池 | 重连后的失败连接可能重新进入池；只读验证异常可能泄漏连接 | 所有失败路径关闭并清空连接，增加回归测试 | 已修复 |
| CR-004 | 高 | `deploy/nginx` | 线上没有通用 `/api/` 代理，新接口会公网 404 | 新增精确 resolver location 和真实 IP 请求头 | 已修复 |
| CR-005 | 高 | `tt-drama-search.js` | 非法字符被静默删除后可能变成真实 ID | 输入阶段不改写；提交时严格拒绝 | 已修复 |
| CR-006 | 中 | `tt-drama-search.js` | 无 AbortController 的 WebView 中超时不能结束 UI 等待 | 使用 `Promise.race` 独立保证 6 秒超时 | 已修复 |
| CR-007 | 低 | CSP | 跨域 preconnect 可能被 `connect-src` 阻止 | 仅放行 static-v1/v2 封面域名 | 已修复 |

## 编译 / 验证结果

- Python resolver/HTTP/契约测试：26 项通过。
- JS URL 与参数契约：35 项断言通过。
- Python 编译、JS 语法和 `git diff --check` 通过。
- 线上只读列排序规则为 `utf8mb4_unicode_ci`；错误大小写在新 SQL 中无结果。
- 线上新 SQL：同一持久连接 5 次为 450.66 / 222.79 / 223.44 / 223.33 / 222.40 ms。
- 本地 390x844 浏览器：非法 ID 不请求；命中后 CTA 参数完整；404 后无 href；无 CSP 警告。
