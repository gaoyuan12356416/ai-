# SA 代码评审

## 结论

本地代码评审与自动化回归通过，设计符合隔离长任务、保持 ready pool 强约束和可恢复执行的目标；systemd 与 SQLite 生产证据待部署后闭环。

## 评审范围

- `features/tt_posts/core.py`
- `features/tt_posts/service.py`
- `scripts/tt_post_prepare_runner.py`
- `deploy/tt-post-prepare.*`
- `deploy/tt-post.env.example`
- `static/tt-post-pool.html`
- 相关 TT Post tests

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | `complete_material_intake` | 插入 ready pool 与 intake ready 必须原子，且更新需带当前 token 条件。 | 保持同一事务，校验 `rowcount=1`，失败整体回滚。 | 已通过 |
| CR-002 | P0 | `claim_material_intake` | reclaim 仅看状态会双领；FIFO 子查询容易遗漏 retry_wait/preparing。 | 候选和 UPDATE 均校验 due/expired 条件；prior 集合覆盖三个活动态。 | 已通过 |
| CR-003 | P0 | public serializer | claim token/lease 暴露会破坏 fencing。 | `_public_material_intake` 删除 token 与 lease；内部 claim 单独返回 token。 | 已通过 |
| CR-004 | P1 | `preparation_process` | 制作前若不重新校验账号能力，ready 成片可能无法发。 | 实时读取账号、设置、Creator Info，并检查最终时长。 | 已通过 |
| CR-005 | P1 | retry | 所有错误都重试会把终态失败卡在账号队首。 | 终态错误集合直接 failed；只对 5xx/未知异常有限重试。 | 已通过 |
| CR-006 | P1 | runner heartbeat | 主调用异常时续租线程可能泄漏。 | context manager + finally 双保险 close；测试线程停止。 | 已通过 |
| CR-007 | P1 | systemd | 长 prepare 若复用发布 runner 或 timeout 太短，会延迟发布/误杀。 | 独立 unit/lock，9600s 大于 9300s，大于 GPU 9000s。 | 已部署验证 |
| CR-008 | P2 | UI | 使用 `innerHTML` 渲染远端名称/错误会引入注入风险。 | 继续使用 `textContent`/DOM 节点安全渲染。 | 已通过 |

## 编译与验证结果

最终由主实现任务填入：

- `py_compile`：5/5 通过
- TT Post 单元/契约/UI 测试：186/186 通过
- GPU/发布链路相关回归：51/51 通过
- 自动化合计：237/237 通过
- `git diff --check`：通过
- 生产 sidecar/systemd/SQLite schema canary：通过
- 真实 TikTok 发布：禁止作为本次验证手段
