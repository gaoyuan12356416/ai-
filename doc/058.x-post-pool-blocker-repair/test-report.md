# 测试报告

## 本地验证（2026-08-28）

- Windows Python 3.14全X回归：`python -m unittest discover -s scripts -p 'test_x*.py' -q`，**832 tests / 56.942s，OK，2项POSIX用例skip**。
- 聚焦账号隔离187项、媒体/OAuth155项、历史恢复CLI6项均通过。
- 首次全量出现新错误码未登记，已修复并重跑全量；无未解决失败。
- 独立代码评审未发现可证实P1/P2；`git diff --check`通过。
- 运行使用模拟X/GPU、本地HTTP和临时SQLite；没有访问真实X发布接口。
- 完整本地日志：`output/x-pool-blockers-20260828/local-x-tests.log`（工作区输出，不入仓库）。

## 生产只读快照与备份

- 备份目录：`/mnt/data-disk/x-post-automation/backups/20260828-pool-blockers-155312`。
- SQLite online backup：quick_check=ok、foreign_key_check=0。
- Run348原13条+Run350原3条：failed、attempt=0、unknown=0、无媒体/帖子ID、未开始发布，relay源/转发尝试均0。
- q533/719/726完整queue/log/relay快照受保护，后续不得清除或重发。
- 15:47:47外部暂停、15:59:58恢复timer；重新门禁后按当前active状态维护部署，16:13:44恢复原状态。

## 生产代码验证

- 最终提交814e168在服务器Python3.9.6：832/832，38.593s，无跳过。
- 备份副本初始化完整性ok/FK0，业务表数量不变。
- 16:13:44代码部署检查完成，Sidecar/Main auth/GPU均200，主API X admin未鉴权401；三个timer恢复active。
- 真实内部只读预检：账号8返回x_post_account_locked，账号19返回x_post_account_needs_review，均409，中文原因指向正确历史日志。
- 保护533/719/726的queue/log/relay逐字段不变；此阶段没有历史队列重置或新增Post/Repost。

## 待完成

16条历史媒体仍在备份副本校验，副本apply/live validate/live apply与自然发布账本验收尚未完成。不能把代码上线或恢复入队视为实际发帖成功。
