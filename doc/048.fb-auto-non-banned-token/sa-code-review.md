# SA 代码评审

## 结论

通过，可以进入 GitHub-first 发布。变更只影响 Page Token 资格查询，不改变
Graph POST、SQLite 状态机或 API 响应结构。

## 评审范围

- `features/fb_auto_posts/repositories.py`
- `scripts/test_fb_auto_repositories.py`
- `scripts/test_fb_auto_publisher.py`
- 需求/API/部署文档一致性

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | 四处 SQL 谓词 | 若 `status` 可为 NULL，`<>1` 不等于“只排除 1” | 只读核对生产字段约束 | 已关闭：NOT NULL、默认 0、NULL=0 |
| CR-002 | P0 | `publisher.execute_next` | 动态状态若沿用计划快照会误发/误跳过 | 每次领取任务后调用 `eligible_credentials()` | 已满足 |
| CR-003 | P0 | Token failover | unknown 若换 Token 会产生重复发帖风险 | 仅 definite failure 轮换，unknown 立即停止 | 已满足 |
| CR-004 | P1 | 历史 run | 重算冻结任务会篡改审计事实 | 不重写既有 run/task | 已满足 |

## 编译 / 验证结果

- `py_compile`：通过。
- Repository + publisher 单元测试：24/24 通过。
- FB 自动发布完整回归：129/129 通过。
- X/TT 合并基线：66/66 通过。
- `git diff --check`：通过（仅 Git 的 LF/CRLF 工作区提示）。
