# 测试报告

## 结论

本地自动化、生产只读 SQL canary、systemd/Nginx/API 与布局验收均通过，发布结论为 GO。已生产部署，未发生真实 X 写入。

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: | ---: |
| 新功能 + app contract + UI | 44 | 44 | 0 | 0 |
| X accounts 回归 | 68 | 68 | 0 | 0 |
| ledger + random relay 回归 | 27 | 27 | 0 | 0 |
| 合计 | 139 | 139 | 0 | 0 |

## 证据

- `python -m unittest scripts.test_x_account_operating_stats scripts.test_x_accounts_app_contract scripts.test_x_membership_duration_ui -v`：44/44。
- `python -m unittest scripts.test_x_accounts -q`：68/68。
- `python -m unittest scripts.test_x_post_ledger scripts.test_x_post_material_random_relay -q`：27/27。
- py_compile、`node --check static/quick-nav.js`、抽取 `x-account-list.html` 内联脚本后 `node --check -`、`git diff --check`：通过。

最终回归首次执行 X accounts 组时出现一次 Windows 本机临时 HTTP `WinError 10053`；未改代码完整重跑后 68/68 通过，其余两组首轮即全绿。

首轮评审、NO-GO 复审及第二轮差异评审均已关闭。生产只读 canary 曾发现 MySQL 5.7 ERROR 1055，现已改为完整 binary Base64 投影别名 GROUP/ORDER，并加入 `ONLY_FULL_GROUP_BY` 合约断言；EXPLAIN 已确认使用 `idx_site_event_time`。systemd canary 还验证并关闭了两项宿主边界：SQL Gate supervisor 只对既有 session-lock 目录增加精确 `ReadWritePaths`；统计 root 单元仅保留只读 `CAP_DAC_READ_SEARCH`，不保留写能力，Token 与 SSH 路径继续不可见。最终 oneshot 结果 success，金额守恒，缓存权限正确。生产静态资产使用安全拦截的管理员/账号样本完成 1600/1280 布局验收：页面无横向溢出，超宽表格仅在容器内滚动，公众指标仅有粉丝/帖子/喜欢。`Intl.NumberFormat` 两位 USD 的极端超大金额仍受浏览器 Number 精度限制。

## 发布建议

GO：GitHub-first、备份、63350 gated SQL canary、systemd/Nginx/API 与布局门禁均已通过；继续禁止用真实 X Post 做回归验收。
