# SA 代码评审

## 结论

通过，可提交发布。未发现阻塞性问题；生产安装 Nginx 配置后仍以 `nginx -t` 通过作为 reload 前置门槛。

## 评审范围

- 固定 W2A origin/path 与核心参数。
- 入口附加参数过滤、编码、重复项和上限。
- DOM 输出、CSP 与开放重定向边界。
- `/tt` 精确 Nginx 映射和缓存/安全响应头。
- Node 契约测试及移动端浏览器行为。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 | 中 | `tt-drama-search.js` | 核心键大小写变体可能作为附加参数混入 | 保留集合使用小写比较 | 已处理 |
| CR-02 | 中 | `tt-drama-search.js` | 追踪 URL 可被超长参数滥用 | 限制 40 项、键 100、值 1024 | 已处理 |
| CR-03 | 中 | `tt-drama-search.conf` | 短路径若用目录跳转可能改变入口地址 | 精确 `location = /tt` 直接 alias HTML | 已处理 |
| CR-04 | 低 | 页面 DOM | 追踪值回显会增加泄露面 | 只显示数量，所有文本使用 `textContent` | 已处理 |

## 编译 / 验证结果

- Python 主服务和既有 sidecar/runner 文件 `py_compile` 通过。
- `static/quick-nav.js` 与 `static/tt-drama-search.js` 的 `node --check` 通过。
- `node scripts/test_tt_drama_bridge.js` 通过，示例链接精确匹配。
- Playwright 390×844 页面无控制台错误或警告，中间页实际导航到预期 W2A URL。
- 2026-07-27 公网复测仍通过，线上静态文件与发布哈希一致，Nginx 和主 API 均为 active。
