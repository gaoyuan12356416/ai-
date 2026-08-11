# SA 代码评审

## 结论

通过。新控制面与既有 X 发布面隔离，最终写入仍归既有 queue/log/token 锁；所有已发现 P0/P1 已修复并有离线回归。首次部署必须保持三道 live gate 为 0，禁止真实 Post canary。

## 评审范围

- `features/x_auto_posts/`、`scripts/x_auto_post_*`、新 systemd/env。
- `features/x_posts/` 与 `features/x_accounts/oauth_service.py` 的增量 auto bridge。
- `app.py`、导航和 `static/x-auto-publish-*`。
- 既有 manual/daily/catchup/schedule/material/drama 发布回归与回滚顺序。

## 问题清单

| 编号 | 严重级别 | 问题 | 修复 | 状态 |
| --- | --- | --- | --- | --- |
| CR-001 | P0 | auto 的 parent/source 校验曾误影响 daily/catchup/schedule | 分离 `require_manual_parent`，仅 auto 专用 route 强制 auto parent | 已关闭 |
| CR-002 | P1 | 确定性预检失败留下 queued/running canonical run，占用现有账号 | 无 queue 时先 `record_failure`，成功后释放 provisional ledger；失败则 retry_wait | 已关闭 |
| CR-003 | P1 | transport unknown 未持久化，可能无限静默对账 | `force_unknown` 写入本地账本；canonical failed 可终态且绝不二发 | 已关闭 |
| CR-004 | P1 | sidecar 崩溃后 queued/publishing run 无安全调用者恢复 | 增加精确 recover：source gate、账号锁 nonblocking、DB fence、迟到线程二次校验 | 已关闭 |
| CR-005 | P1 | live gate 关闭会连对账一起停止 | 闭门只领取 reconcile-only；禁止 selection/plan/publish | 已关闭 |
| CR-006 | P1 | 发布桥接超时短于 X 分片上传，可能提前释放共享锁 | 查询/发布超时拆分为 120/9000 秒，强制 `publish+300≤execute`、`execute+300≤lease` | 已关闭 |
| CR-007 | P1 | auto actor 请求结构与桥接校验不一致 | 客户端固定字符串，服务端固定内部审计身份，不信任请求用户 | 已关闭 |

## 编译 / 验证结果

- Python `py_compile` 和 5 个 JS `node --check` 通过。
- X auto、bridge、UI/代理、TT 兼容套件 162/162 通过。
- 既有 X manual/daily/schedule/pool/account 套件 244/244 通过。
- X accounts app contract 28/28 通过。
- 所有发布 HTTP 均为 fake/mock；未连接真实 X 写接口。
