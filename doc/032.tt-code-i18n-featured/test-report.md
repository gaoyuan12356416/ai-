# 测试报告

## 测试结论

本地功能与回归测试通过，代码评审无未解决 P0/P1/P2。可以发布到生产做最终线上验收。

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

## 缺陷情况

- `BUG-001`：MySQL 5.7 不支持非捕获组正则，已改为兼容表达式并回归通过。
- 代码评审发现的 5 个问题均已修复；无开放缺陷。

## 验证证据

- `node scripts/test_tt_drama_code_bridge.js`：148 项通过。
- `node scripts/test_tt_drama_code_browser.js`：21 项通过。
- `python -m unittest tests.test_tt_drama_featured_service tests.test_tt_drama_resolver_app_contract`：42 项通过。
- `node scripts/test_tt_drama_bridge.js`：53 项通过。
- `python -m compileall -q features scripts tests`、`git diff --check`：通过。

## 遗留风险

- 全仓 481 项中 3 项既有失败均来自 `test_ad_control_v3_routes` 的 GET/POST/DELETE 顺序断言；本需求未修改 `app.py` 或该测试。
- 生产消费查询在只读 canary 中约 2.3–2.5 秒，但首次生产完整刷新仍需现场计时并确认低于 15 分钟。

## 发布建议

按 GitHub-first 最小范围部署；不切换 `/opt/tt-post/current`，先备份再更新静态/Nginx/systemd/资源缓存 release，并完成线上多语言与旧 `/tt` 双重验收。
