# 测试报告

## 测试结论

本地功能、兼容、安全和真实浏览器回归通过。生产 Nginx、HTTP 响应头、
真实数据 WebP 与冷/热启动验收在 GitHub-first 部署后补充。

## 测试范围

- 23 语言静态首屏、请求头白名单与无 JS 首屏。
- 单语言 schema v3、WebP 生成/回退、LKG 与全量基础设施失败。
- 横向滑动、拖动抑制、Search/Featured 完整 W2A 跳转。
- 四字符 code 唯一性、`{code}` 冻结/重试和 Redis/SQLite resolver 回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Featured Python 新/旧 | 43 | 43 | 0 | 0 |
| TT 发布/code 宏核心 | 237 | 237 | 0 | 0 |
| TT 链接/app 合同 | 22 | 22 | 0 | 0 |
| 新/旧前端合同 | 250 | 250 | 0 | 0 |
| 真实 Chrome | 44 | 44 | 0 | 0 |
| 合计 | 596 | 596 | 0 | 0 |

## 缺陷情况

代码评审提出 5 项问题，均在部署前修复并通过独立复核；无开放缺陷。

## 验证证据

- 构建器输出 23 个语言 HTML、唯一 hash JS
  `tt-drama-code-search.e907e1e2a988.js`。
- Chrome 覆盖英文、简中、繁中、阿语、未知语言、390x844、双入口、
  header/navigator 不一致、Search 与 Featured 拦截跳转。
- Python 覆盖真实 236x338 WebP、单图失败、全部失败、Pillow/WebP
  预检、原子写失败、私有字段与路径穿越。
- `test_automatic_schedule_freezes_code_macro_before_gpu_publish`、
  `test_queue_allocates_code_and_freezes_exact_code_macro` 等既有宏测试通过。

## 遗留风险

- 公网耗时仍受用户网络 RTT 影响；本次验收关注消除语言二次切换、缩小
  传输量和热访问复用，而不承诺固定毫秒值。
- 生产 Nginx 1.14.1 及真实封面 CDN 必须在切流前通过精确验收。

## 发布建议

本地门禁通过。允许提交并推送 GitHub；只有生产备份、Pillow smoke test、
资产生成、`nginx -t` 全部通过后才切换 `/tt`。
