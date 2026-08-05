# 测试报告

## 测试结论

本地功能、回归与生产验收均通过，代码评审无未解决 P0/P1/P2；需求已发布。

## 测试范围

- 23 套 UI 语言、浏览器语言优先级、默认英文及 RTL。
- schema v2 分语言榜单、Top 选择、资源合并、安全校验、原子发布和 LKG。
- 搜索 code/剧 ID、Featured 单击/拖动与 `Search`/`Featured` 渠道参数。
- 旧 `/tt` 前端合同与相关 Python 后端合同。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 跳过 | 阻塞 |
| --- | --- | --- | --- | --- | --- |
| 新 `/tt-code` Node 合同断言 | 148 | 148 | 0 | 0 | 否 |
| 真实 Chrome 场景检查 | 21 | 21 | 0 | 0 | 否 |
| Python 定向单元测试 | 42 | 42 | 0 | 0 | 否 |
| 旧 `/tt` Node 合同断言 | 53 | 53 | 0 | 0 | 否 |
| 全仓 unittest 基线 | 481 | 477 | 3（既有） | 1 | 否 |
| 生产真实 Chrome 场景检查 | 21 | 21 | 0 | 0 | 否 |
| 生产 v2 schema/内容门禁 | 110 items | 110 | 0 | 0 | 否 |

## 缺陷情况

- `BUG-001`：MySQL 5.7 不支持非捕获组正则，已改为兼容表达式并回归通过。
- 代码评审发现的 5 个问题均已修复；无开放缺陷。

## 验证证据

- `node scripts/test_tt_drama_code_bridge.js`：148 项通过。
- `node scripts/test_tt_drama_code_browser.js`：21 项通过。
- `python -m unittest tests.test_tt_drama_featured_service tests.test_tt_drama_resolver_app_contract`：42 项通过。
- `node scripts/test_tt_drama_bridge.js`：53 项通过。
- `python -m compileall -q features scripts tests`、`git diff --check`：通过。
- 公网 v2：24,274 bytes，`source_date=2026-08-04`，22 个语言桶，每桶 5 条、110 个全局唯一 ID，无 `spend` 字段。
- 生产 Chrome：`en-US`、`zh-CN`、`zh-TW`、`ar`、`bn-BD` 共 21 项通过；封面 200 并正常渲染。
- Featured 单击：真实 resolver 仅请求一次，目标 `af_channel=Featured`；拖动不跳转。
- 搜索：生产 Featured 剧 ID `zALq8tHA9a` 解析成功，目标 `af_channel=Search`。
- 兼容：旧 v1 端点 5 条，旧 `/tt` 200；TT 应用 current 未变化。
- 服务：12:17:03–12:17:16 刷新成功，timer `active/waiting`，下一次 15:30 CST。

## 遗留风险

- 全仓 481 项中 3 项既有失败均来自 `test_ad_control_v3_routes` 的 GET/POST/DELETE 顺序断言；本需求未修改 `app.py` 或该测试。
- 首次生产刷新遇到一次只读数据库 `OperationalError`；LKG 正常保护，精确查询和一次受控重试均成功。定时任务每天 15:30/18:00 各运行一次，单次失败不会破坏现有榜单。

## 发布建议

已按 GitHub-first 最小范围部署并完成多语言与旧 `/tt` 双重验收；维持现有定时器并观察下一次自然刷新日志。
