# SA 代码评审

## 结论

关键问题修复后通过，待完整测试和生产 canary 验证。

## 评审范围

SQLite 迁移、排期状态机、短剧选择器、runner、sidecar/后台 API、页面、权限、systemd/nginx 和测试。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 | P0 | `due_schedule_slots` | 只查当前分钟无法恢复 60–90 秒内和已冻结批次 | 枚举窗口分钟并返回同日冻结 backlog | 已修复 |
| CR-02 | P0 | timers | 长 worker 持锁会漏认领下一分钟 | 新增 claim timer `:00`，worker `:10` | 已修复 |
| CR-03 | P1 | drama failure | 预检错误未绑定短剧池 | error 携带 pool/content，同事务 needs_review | 已修复 |
| CR-04 | P1 | run sync | 短剧首条失败后批次仍非终态 | 短剧已知失败置 stopped | 已修复 |
| CR-05 | P1 | app audit | 核心写成功、审计失败会误返 400 | 审计 best-effort 并返回 audit_recorded | 已修复 |
| CR-06 | P1 | UI | 状态值和剧集字段与 API 不一致 | 对齐 active/publish_status/run_date 等字段 | 已修复 |
| CR-07 | P1 | navigation | 两个池默认仅管理员 | 改为 false，后端继续按动态配置鉴权 | 已修复 |
| CR-08 | P1 | source description | 真实数据含换行，严格控制字符校验会误拒绝 | 仅描述折叠空白，NUL 仍拒绝 | 已修复 |
| CR-09 | P2 | HTML cache | 新页面可能命中旧缓存 | nginx exact location no-store | 已修复 |
| CR-10 | P1 | frozen account verify | claim 后移除账号会被当前配置 scope 拒绝，冻结批次无法恢复 | 内部 scope 合并 claimed/queued/running 批次账号 | 已修复 |

## 编译 / 验证结果

- 聚焦测试 70/70 通过。
- Python 编译通过。
- `git diff --check` 通过。
- 合并生产基线后完整 `test_x*.py` 288/288 通过；TT source-cache Python/Node 回归通过。
