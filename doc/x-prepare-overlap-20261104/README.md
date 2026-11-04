# X 制作空闲修复

## 问题与范围

2026-11-04 10:57 北京时间，24 条 X 待制作中：14 条与正在发布的账号冲突，2 条与正在制作的账号冲突，4 条等待更早的发布，4 条冻结为 CPU 配置。另有 6 条图片进入随机视频模板，原生校验失败被转换成 503，checkpoint 阻塞后续失败回执，租约到期后再次领取。原始队列为 5591、5595、5612、5618、5674、5707。

仅调整协调器，不修改素材、模板、凭据、成片、容量或历史暂停数据。保留线上其他修复及首页表头。基线提交保留实际运行的 core/service/adapters 叠加修复。

## 设计及风险检查

- 只有 X 已冻结 queue 制作、无写入/旧执行/未知结果证据的任务使用 `prepare:<account resource>`。同账号和共用 relay 的制作仍单条互斥。
- 允许与健康、持有有效租约的发布并行。原账号发布锁、legacy 锁、unknown/quarantine 锁不解除。原来已运行的制作继续保留旧锁直到自然结束。
- 发布不越过同预约时间更早的原生 queue 制作，顺序比较原生 queue ID，避免制作完成后新增的发布工作项 ID 造成错误顺序。原有发布之间的排序不变。
- 制作锁不能申请平台写入凭据。制作结束/失败/租约到期只清理自己的代次资源。
- 仅处理原生明确报错 `x_random_requires_video` 和 `x_post_auto_template_duration_exceeded`；先验证中央无任何操作/已完成回执、原生无尝试/未知/成片/发布成功，再写原生失败账本，读回失败后原子保存中央失败和拒绝回执。
- 首次 checkpoint HTTP 422 使现有节点停止执行；不隐式重试的旧客户端会进入失败回执路径。原样后台重交 ACK 的是已持久化的拒绝处理结果。旧代次只允许相同输入回执/无写入失败回执确认送达，不修改当前状态，不创建成功记录。拒绝结果后不自动重新制作或发布。
- 网络失败、凭据故障、不同回执、已有写入和未知结果不走上述确定失败路径。

## 验证

`python -m unittest scripts.test_pipeline_x_prepare_overlap scripts.test_pipeline_dispatch_core scripts.test_pipeline_coordinator scripts.test_pipeline_failed_receipt_ack scripts.test_pipeline_receipt_outbox scripts.test_pipeline_worker scripts.test_pipeline_x_native scripts.test_pipeline_x_recovery_concurrency scripts.test_pipeline_prepare_recovery scripts.test_pipeline_late_source_artifact scripts.test_pipeline_cutover scripts.test_pipeline_capacity_modes -q`

测试使用临时 SQLite、真实 HTTP handler/客户端/outbox 及原生 X 账本，模拟网络边界，不向平台创建测试帖子。覆盖双节点争抢、两阶段重叠、原生顺序、未知结果、旧占用、失效领取、重复/错误身份回执、确定失败收敛及网络异常保留。

## 部署及回滚

GitHub 提交后，服务器获取确切提交，部署 `core.py` 与 `service.py` 两个文件。脚本对现网逐文件校验基线，数据盘备份原文件及协调库，只对协调器短暂阻断新 HTTP 连接并排空处理线程，再重启协调器。75 秒自动撤销网络门禁。CPU/GPU 节点、X 原生服务、主 API 及前端不重启，不调整并发。

使用 `scripts/deploy_x_prepare_overlap.py --sha <release> --expected-sha <baseline> --apply`；两份部署脚本必须来自同一提交且位于同目录。

回滚先通过容量管理停止新的制作领取，等待 `prepare:%` 锁自然排空。再运行备份目录中的 `deploy_x_prepare_overlap.py --rollback <backup-directory> --apply`。脚本拒绝在新制作锁仍被占用时回退。只恢复两个文件；不恢复数据库、失败处理记录、发布回执、凭据或成片。恢复管理员保存的接单设置后确认业务账本与节点心跳。切勿直接恢复旧数据库。


## 上线记录

- 代码提交 `ed2561faa6df8b410a95efc04b786f295f604df5`；现场基线 `4b823366191e3f8f08f13ecfd4cfc02e3fa3478e`。已推送 GitHub，CPU 服务器已 fetch 确切版本并逐文件核对。
- 2026-11-04 11:16:37 北京时间开始备份；协调器 HTTP 排空门禁 7.007 秒，PID 4075066 → 2068480，health 200，门禁已撤销。CPU 节点、X 原生服务、主 API PID 均未变；GPU 节点 PID 267919，自 11 月 3 日 19:18:51 起持续运行。
- 仅部署 `/opt/pipeline-dispatch/current/features/pipeline_dispatch/{core,service}.py`；实际 runtime `/mnt/data-disk/pipeline-dispatch-runtime/x-publish-recovery-a97fc23/candidates/coordinator`。
- 备份 `/mnt/data-disk/pipeline-dispatch/x-prepare-overlap-20261104T031637Z`，包含原文件、manifest、部署结果及在线 SQLite 备份。数据备份仅用于审计，不用于回滚恢复覆盖当前事实。
- 前后容量均为 version 113，配置 SHA256 `f83703bfa891be2d80e5b690a104daef2d1b766a1458e4b93e977d69a349cda9`。CPU 制作 4/发布 24，GPU 制作 12/发布 12；保留管理员当前已保存值。
- 11:17:03：GPU 制作运行 11 条；7 组同账号制作/发布同时持锁。11:17:57 GPU 上 8 个 FFmpeg 都使用 NVENC，GPU 运算利用率 35%、编码器 82%，显存 4526/15360 MiB。这里只证明调度恢复与正在渲染，不将领取数量当作完成量。
- 11:18:43：6 条已知循环任务均为 `failed/x_random_requires_video`，代次分别 257、130、103、54、283、31。没有重新选材或新建发布任务；X 待制作 6 条、运行制作 14 条（GPU 11、CPU 3）；上线后中央发布账本新增 3 条 X 确认成功。
- 本次完整离线回归 147 项通过；新测试覆盖真实 HTTP/outbox、原生 SQLite 失败收敛、历史代次回执和原操作意图保护。线上使用自然任务验证，未创建测试帖子。
- 11:19:27：原有 17 条在途发布中 3 条已成功、14 条持续运行，全部领取代次不变、无错误；8 组同账号制作与发布正在重叠。剩余 6 条中央排队记录中，5 条等待同账号的制作完成，另 1 条受已有未知发布结果保护（应在首页异常中展示）。6 条图片任务的原生发布日志均为 failed、attempt_count=0、unknown_outcome=0、无 Post ID。
- 精确代码回退：停止新的制作领取并等待新制作锁自然排空后，运行 `/usr/bin/python3.9 /mnt/data-disk/pipeline-dispatch/x-prepare-overlap-20261104T031637Z/deploy_x_prepare_overlap.py --rollback /mnt/data-disk/pipeline-dispatch/x-prepare-overlap-20261104T031637Z --apply`。保留当前数据库，回读并恢复管理员接单设置。
- 技能上下文未修改；本次行为、验证及回滚记录保存在仓库文档，避免改变其他对话的共享指导文件。
