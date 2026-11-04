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
