# 测试报告

## 结论

本地自动化通过，建议在备份和真实只读 SQL canary 门禁完成后部署。未生产部署或真实 X 写入。

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: | ---: |
| 新功能 + app contract + UI | 40 | 40 | 0 | 0 |
| X accounts 回归 | 68 | 68 | 0 | 0 |
| ledger + random relay 回归 | 27 | 27 | 0 | 0 |
| 合计 | 135 | 135 | 0 | 0 |

## 证据

- `python -m unittest scripts.test_x_account_operating_stats scripts.test_x_accounts_app_contract scripts.test_x_membership_duration_ui -v`：40/40。
- `python -m unittest scripts.test_x_accounts -q`：68/68。
- `python -m unittest scripts.test_x_post_ledger scripts.test_x_post_material_random_relay -q`：27/27。
- py_compile、`node --check static/quick-nav.js`、`git diff --check`：通过。

评审发现 4 项均已关闭，无遗留 bug 文件。真实表 `c` 基数/查询耗时/总金额、systemd/Nginx 和登录态视觉验收待部署阶段完成。`Intl.NumberFormat` 两位 USD 的极端超大金额仍受浏览器 Number 精度限制。

## 发布建议

条件 GO：仅在 GitHub-first、备份、63350 gated SQL canary、systemd/Nginx 验证通过后上线；禁止用真实 X Post 验收。
