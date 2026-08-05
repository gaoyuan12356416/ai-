# SA 代码评审

## 结论

通过。独立复核未发现未解决的 P0、P1 或 P2 问题，可以进入 GitHub-first 发布流程。

## 评审范围

- `static/tt-drama-code-search.html` 与 `static/tt-drama-code-search.js`：语言解析、动态文案、RTL、Featured 点击/拖动及安全校验。
- `features/tt_drama_featured` 与刷新脚本：只读查询、按语言 Top、资源解析、schema v2、原子发布和 last-known-good。
- Nginx/systemd：新静态端点、超时、缓存文件路径和旧 `/tt` 隔离。
- 自动化测试及 032 需求、接口和测试文档的一致性。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | 前端榜单语言解析 | 地区语言未优先命中精确桶 | exact 命中后再回退 base | 已修复 |
| CR-002 | P1 | 前端 UI/榜单联动 | 将来新增未翻译语言桶时可能出现英文 UI 加该语言榜单 | 榜单候选仅允许已支持 UI 语言 | 已修复 |
| CR-003 | P2 | 需求与接口文档 | 曾残留旧路径/schema 描述 | 统一为 `current-by-language.json` 和 schema v2 | 已修复 |
| CR-004 | P1 | 前后端桶名校验 | 两端语言桶正则边界不一致 | 前端使用与后端一致的规范桶正则 | 已修复 |
| CR-005 | P2 | 阿拉伯语 RTL 排版 | 通用负字距不适合阿拉伯字形 | RTL 标题及标签明确重置为零字距 | 已修复 |

## 编译 / 验证结果

- 新 `/tt-code` Node 合同测试：148 项通过。
- 真实 Chrome 回归：21 项通过，含英语、简中、繁中、阿拉伯语、未知语言、点击和拖动。
- Python 定向测试：42 项通过。
- 旧 `/tt` Node 回归：53 项通过。
- `python -m compileall -q features scripts tests` 与 `git diff --check` 通过。
- 全仓 481 项测试中 3 项失败、1 项跳过；3 项均为既有 `test_ad_control_v3_routes` 失败，本需求未修改相关文件，不作为本次发布阻断项。
