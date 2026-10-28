# 部署与回滚

状态：编写时尚无生产切换或真实首批回执。发布负责人补充准确 SHA、备份点及验收结果。

## 目标

CPU 43.166.187.96，运行目录 /mnt/data-disk/meta-violation-material-cleanup。GitHub gaoyuan12356416/ai-，releases/<sha> 为固定提交，current 指向该发布。

新增 meta-cover-scan.service/.timer、meta-cover-worker.service、meta-cover-notify.service/.timer。安装器 deploy/install_cover.py 提供 prepare/apply/rollback；参数以对应提交 --help 为准。

旧 meta-violation-material-cleanup.timer 保持停用。保留旧代码、任务、凭证和历史回执；新流程不调用旧 run.py live 删除入口。

## 发布顺序

1. 完成本地测试和 diff 检查，提交并推送 GitHub，记录 SHA。
2. 核实服务器版本、进程和锁。停止旧任务的新调度，等待在途写入排空并确认旧进程退出。
3. 从 GitHub 拉取精确 SHA；prepare 备份代码链接、配置、unit/timer 和账本，SQLite 使用一致性备份。
4. apply 切换并安装新单元；首次验收前保持持续消费关闭。配置 cover_live_enabled，旧删除 timer 不启动。
5. 独立 preview 核对源范围、语言、完整链接及 payload；scan 入队，work --once --max-ads 256 处理自然命中首批。
6. 保存峰值/吞吐/限流指标、绑定读回、删除意图与回执、未知隔离及 Feishu 回执。确认无重复写入或参数漂移再启动持续 worker 和小时 scan timer。
7. 读取服务/定时器状态与下一触发时间、剩余积压，补充发布回执。

32 是上限，单账户 2、24 次/秒、账户分布和限流会影响实际并发。零候选必须记为未完成真实写入验证，不能称作成功执行。

异常重复写入或参数变化触发 cover-write-paused.json。排查只读核实，不通过清账本或删除围栏强制重放。

## 回滚

停止新 scan/notify timers 和 worker，确认在途请求排空；使用 installer rollback 或恢复备份代码链接、配置和单元。继续保留共享 Store 的增量互斥能力，避免旧入口忽略新未知围栏。旧删除 timer 保持停用。

保留最新 cover.sqlite3、共享 tasks.sqlite3、cover-audit 和未知状态；绝不以旧 SQLite 备份覆盖进度。成功绑定不会随代码回滚自动恢复；已删除 Creative 无法恢复旧 ID。业务素材恢复需另行制定可核实操作。

## 发布回执（执行后填写）

- GitHub SHA、服务器 current、旧版本：
- 备份点与回滚命令：
- 首批自然命中、处理数、积压：
- 替换成功/失败/未知：
- 删除成功/失败/未知/引用暂缓：
- Graph 实际峰值、吞吐、限流：
- Feishu 回执：
- 新服务/timer 状态和旧删除 timer 停用证据：
