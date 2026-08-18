# 测试报告

## 结论

本地自动化通过，建议在备份和真实只读 SQL canary 门禁完成后部署。未生产部署或真实 X 写入。

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

首轮评审、NO-GO 复审及第二轮差异评审均已关闭。生产只读 canary 曾发现 MySQL 5.7 ERROR 1055，现已改为完整 binary Base64 投影别名 GROUP/ORDER，并加入 `ONLY_FULL_GROUP_BY` 合约断言；EXPLAIN 已确认使用 `idx_site_event_time`。首次 systemd canary 还验证了宿主 SQL Gate 的 supervisor 必须在既有 `/var/lib/sql-connection-gate/session-locks` 获取清理锁，因此在 `ProtectSystem=strict` 下只对该目录增加精确 `ReadWritePaths`，其他 gate 状态与配置路径仍只读。修复后的 systemd canary、登录态视觉验收仍待部署门禁重验。`Intl.NumberFormat` 两位 USD 的极端超大金额仍受浏览器 Number 精度限制。

## 发布建议

条件 GO：仅在 GitHub-first、备份、63350 gated SQL canary、systemd/Nginx 验证通过后上线；禁止用真实 X Post 验收。
