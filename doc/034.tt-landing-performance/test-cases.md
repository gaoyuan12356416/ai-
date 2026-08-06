# 测试用例

## 测试范围

静态构建、locale 选择、gzip/cache、单语言榜单、WebP 回退、页面交互、W2A 跳转和生产冷/热启动。

## 测试数据

- locale：`en-US`、`zh-CN`、`zh-TW`、`ar-SA`、不支持的 `nl-NL`
- 5 条合法 Featured、缺桶、过期快照、非法语言路径、缩略图成功/超时/超限/解码失败
- 搜索：一个已发布 code、一个 Content ID、一个无发布记录 Content ID

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 简中首屏 | `Accept-Language: zh-CN` | GET `/tt` | 响应 HTML 已是简中，`Content-Language=zh-hans` | P0 | 通过 |
| TC-002 | 繁中首屏 | `Accept-Language: zh-TW` | GET `/tt` | HTML 为繁中，`lang=zh-Hant` | P0 | 通过 |
| TC-003 | RTL 首屏 | `Accept-Language: ar-SA` | GET `/tt` | 阿拉伯语且 `dir=rtl` | P0 | 通过 |
| TC-004 | 不支持语言 | `Accept-Language: nl-NL` | GET `/tt` | 使用英文，无 5xx | P0 | 通过 |
| TC-005 | 语言不一致 | 请求头与 navigator 不一致 | 浏览器打开 | 服务端 locale 保持权威，JS 不产生二次可见切换 | P1 | 通过 |
| TC-006 | 首屏无英文闪屏 | 冷缓存/慢网络 | 记录 DOM 与 FCP | FCP 前后标题均为目标语言 | P0 | 通过 |
| TC-007 | hash JS | 发布静态资产 | 检查 HTML/响应头 | URL 含 hash，immutable，一年缓存 | P0 | 通过 |
| TC-008 | gzip | 客户端接受 gzip | GET HTML/JS/JSON | 三类响应均 gzip | P0 | 通过 |
| TC-009 | HTML no-store | 任意 locale | GET `/tt` | 继续 no-store，避免陈旧路由 | P0 | 通过 |
| TC-010 | 骨架卡 | 禁止主 JS | 打开 HTML | 已有 5 张不可点击骨架卡 | P1 | 通过 |
| TC-011 | 单语言接口 | `en` | GET locale JSON | schema v3、恰好 5 条、不含 spend | P0 | 通过 |
| TC-012 | 页面请求范围 | 中文浏览器 | 观察网络 | 只请求一个 `zh-tw.json`，不取全量 JSON | P0 | 通过 |
| TC-013 | locale LKG | 重复相同快照 | 连续写入 | 第二次不替换文件 | P1 | 通过 |
| TC-014 | 非法路径 | `../../en` | 请求 API/封面 | 404，不读取任意文件 | P0 | 通过 |
| TC-015 | WebP 成功 | 合法 JPEG | 运行生成 | 236x338 WebP、hash 文件名、公开 200 | P1 | 通过 |
| TC-016 | 图片超限 | >最大字节/像素 | 运行生成 | 拒绝缩略图并回退原 URL | P0 | 通过 |
| TC-017 | 图片网络失败 | 超时/5xx | 运行生成 | 其他语言/图片继续，快照仍可用 | P0 | 通过 |
| TC-018 | Featured 超时 | JSON 延迟 >4s | 打开页面 | 保留骨架/占位，搜索可用 | P0 | 通过 |
| TC-019 | code 搜索 | 合法 code | 搜索并拦截跳转 | 完整 frozen 归因，`af_channel=TT` | P0 | 通过 |
| TC-020 | Content ID 搜索 | 合法 ID | 搜索并拦截跳转 | 最新发布归因，`af_channel=Search` | P0 | 通过 |
| TC-021 | Featured 点击 | 动态卡片 | 单击并拦截跳转 | resolver 验证后跳转，`af_channel=Featured` | P0 | 通过 |
| TC-022 | 拖动抑制 | 横向拖卡片 | 释放 | 不误跳转，箭头/滑动仍可用 | P1 | 通过 |
| TC-023 | 双入口一致 | `/tt`、`/tt-code` | 对比功能与 hash | 相同 locale 和行为 | P0 | 通过 |
| TC-024 | 尾斜杠 | `/tt/`、`/tt-code/` | GET | 继续 404，无重定向 | P0 | 通过 |
| TC-025 | `{code}` 回归 | TT 发布测试集 | 运行既有测试 | 宏冻结/重试语义全部通过 | P0 | 通过 |
| TC-026 | 冷热性能 | 390x844、zh-CN | 各运行至少 3 次 | 冷启动无语言二次切换，热启动复用 hash JS | P0 | 通过 |

## 回归范围

- `tests/test_tt_drama_featured_service.py`
- `scripts/test_tt_drama_code_bridge.js`
- `scripts/test_tt_drama_code_browser.js`
- `scripts/smoke_tt_drama_code_production.js`
- TT Post code/macro 合同测试
- Nginx `nginx -t`、线上 HTTP 头和真实 Chrome 点击流
