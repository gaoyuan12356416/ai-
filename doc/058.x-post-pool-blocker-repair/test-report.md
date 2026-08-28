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
- 15:47:47三个X timer被暂停，原因待确认；目前保持其停止状态，未自动启动发布。

## 待完成

Linux全量测试、备份副本初始化/恢复演练、exact commit部署、live恢复与自然发布结果尚未完成，不能把本地测试或入队视为实际发布成功。
