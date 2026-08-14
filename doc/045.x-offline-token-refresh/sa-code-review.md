# SA 代码评审

## 结论

通过，可提交并进入生产备份/部署。刷新网络调用位于账号锁内，最终 X 写入仍位于 `publish_credentials(...)` 锁内；没有 Token 内容进入 DTO、DOM 或日志。

## 评审范围

OAuth 状态投影、刷新原子写、发布前校验、X Auto Run 任务快照、Relay source/target、两套 UI、测试与部署边界。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | `status_for` / `row_to_item` | 过期可续期与永久失效必须可区分 | 新增四个安全字段；仅 `status=active` 可进入发布 | 已解决 |
| CR-002 | P0 | `verify_account` | `only_refresh_required` 不得因新状态投影而跳过已过期 Token | 改为直接检查 Access Token 缺失和 120 秒到期窗口 | 已解决 |
| CR-003 | P0 | `publish_queue_request` | 只在预约前刷新不足以覆盖冻结队列或延迟上传 | source、短视频、长视频和 relay target 均增加最终保护 | 已解决 |
| CR-004 | P0 | Token 文件 | 轮换写入不得改变 owner/mode 或恢复旧 Refresh Token | 复用既有 `atomic_write_json`/目录 owner 对齐；回滚保留现有凭证 | 已确认 |
| CR-005 | P1 | X Auto | Preview 与真实执行副作用必须分离 | Preview 保持 `_account_snapshot`；`_run_tasks` 才调用 verify | 已解决 |

## 编译 / 验证结果

- `python -m compileall -q features scripts`：通过。
- `python -m unittest discover -s scripts -p "test_x*.py"`：667 项，665 通过、2 项既有条件跳过、0 失败。
- `node --check` 与两页内联脚本 `vm.Script`：通过。
- `git diff --check`：通过。
