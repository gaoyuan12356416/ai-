# SA 代码评审

## 结论

本地代码评审通过。发现的 P0/P1 问题均已修复并增加回归测试；生产迁移与无真实发帖验收完成后方可发布结项。

## 评审范围

- `features/x_posts/` 的 additive schema、高优排序、手动批次和发布账本。
- `features/x_accounts/` 与 `app.py` 的角色权限、DTO、CSRF/method 和账号身份边界。
- `scripts/x_post_manual_runner.py` 的全批预检、恢复、停止语义和共享锁。
- 两个池页面、发布日志页、systemd 单元以及 X 全回归。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | `app.py#do_PUT` | 直接委托整个 `do_POST` 会让其他 POST 路由意外接受 PUT | PUT 仅对白名单高优路径委托，其余 404 | 已修复并测试 |
| CR-002 | P0 | `get_manual_run` | 把日志瞬态 `media_uploading/post_creating` 当队列状态返回，恢复解析会失败 | 只返回冻结 queue 状态，未知标记独立返回 | 已修复并测试 |
| CR-003 | P0 | `claim_manual_run` | 进程中断在 `publishing` 时 run 可能长期保持 running | 下一次领取原子转为 stopped/needs_review，禁止重试 | 已修复并测试 |
| CR-004 | P1 | `_public_manual_run` | 删除已知敏感字段但复制所有未来字段，不是严格安全 DTO | run/queue 均改为显式字段白名单 | 已修复并测试 |
| CR-005 | P1 | `record_drama_pool_checks` | 高优短剧后续校验失败时可能残留高优徽标 | 校验失败事务内清空全部高优字段，并防御性约束排序 | 已修复并测试 |
| CR-006 | P2 | `x-post-logs.html` | 手动队列在历史日志中显示为“测试批次” | 增加手动/定时批次标识 | 已修复并测试 |

## 编译 / 验证结果

- Python 编译：通过。
- X 相关离线回归：通过，使用临时 SQLite/mock，未调用真实 X 写接口。
- 前端内联 JavaScript 语法与 DOM 合同：通过。
- `git diff --check`：无空白错误（Windows 行尾提示不影响内容）。
