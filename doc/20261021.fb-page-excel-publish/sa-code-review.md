# SA 代码评审

日期：2026-10-21。应用代码、部署脚本与回滚流程独立评审通过，可合入。发现的应用问题均已修正，部署恢复边界已补充守卫并验证；目前没有待修复的阻塞项。上线状态以 deploy.md、test-report.md 为准。

## 范围与方法

独立检查 Excel 解析、预检/提交/查询、core 冻结与领取任务、来源批量解析、池入队、个人授权仓库、执行/对账、Graph 标题、SQL broker 白名单、前端恢复流程。读取真实实现和新增测试，并新增 `scripts/test_fb_excel_review.py` 补测跨模块行为；不改运行代码，不访问生产 Graph。

## 问题闭环

| 编号 | 级别 | 位置 | 发现及修复 | 状态 |
| --- | --- | --- | --- | --- |
| BUG-001 | P2 | excel.py/read_workbook | max_col=9 导致 J 列后的数据/公式漏检；现读取实际全列、补足缺失尾列，并限制扫描单元格总量 | 修复前 3 个独立用例失败，修复后通过；详见 bugs/BUG-001.md |
| REVIEW-02 | P3 | fb-video-publish.js/admissionMeta | skipped ID 截断到 128 字符后无法匹配原长输入，导致未添加行丢失；现直接从原输入移除已成功 ID，保留完整未添加项并去重保序 | 已复读修复；前端代理新增 160 字符输入用例，隔离浏览器 42/42 通过 |

## 关键核验

| 关注点 | 结论 |
| --- | --- |
| 完整预检事实 | 验证 actor、完整 request、来源、配置和历史签名后才过滤未确认历史行；不能用旧集合的签名伪造子集 |
| 幂等与事务 | request_hash 绑定原 request 与 allow_repeat；4 个并发相同操作仅建一批。冻结第二行注入故障后 batch/task/link 全回滚，原操作可安全重试 |
| 每行执行 | processing_mode、策略、标题及行号进入快照；时间排序后分配 page_sequence。真实领取/执行测试覆盖逆序预约、相同时间行序及回执未完成时阻塞后续 |
| 权限和 Token | 新路由要求 Page 管理入口；新查询不能读取普通手动批次。个人授权按可信内部用户映射 Facebook 身份，执行/对账均限制该范围，缺失不回退 |
| 既有共享保护 | Excel 复用跨模块 Page 锁、素材占用、配额、短链及未知结果屏障；旧任务缺少策略仍默认 page_pool，旧公开提交拒绝 schema 3 |
| 部分结果 | 未配置 Page 只跳过并提示，未自动创建；逐项来源错误可继续，SQL/签名/并发条件变化整体拒绝且无部分入队 |
| 前端恢复 | 发送前保存原操作，首次明确拒绝后才释放；未知请求的后续拒绝不能清除原 pending。入池保留未添加原输入 |
| 回滚入口 | 关闭 Excel 新增入口不阻止已有任务执行/对账，也不阻止无需重新映射的原操作查询；不能将此入口开关等同于可回退旧 worker |

## 实际验证

- `python scripts/test_fb_excel_review.py -v`：13/13 通过，临时 SQLite、本地工作簿、受控时钟和模拟 Graph。
- `python -m unittest discover -s scripts -p 'test_fb_excel*.py' -q`：最终 76/76 通过，包含上述独立用例、Excel/个人授权及 24 项部署测试。
- `git diff --check -- scripts/test_fb_excel_review.py doc/20261021.fb-page-excel-publish`：通过。
- 前端浏览器结果由前端代理执行并回传，完整证据进入 test-report.md；此评审未将模拟结果写为真实平台发布成功。

## 部署与回滚复审

审阅 `scripts/deploy_fb_excel_publish.py`、其调用的排空/状态恢复函数及 `scripts/test_fb_excel_deploy.py`。已独立运行最终部署测试及完整 Excel 测试；没有执行线上部署。

| 边界 | 核验结论 |
| --- | --- |
| 41 个目标 | 8 个 video Python 双副本、3 个 auto Python 仅 FB、副本各 3 份的 7 个静态资源、1 个 drop-in；无 app.py、DB、短链或 GPU 目标 |
| 精确来源与基线 | 固定 GitHub ref/HEAD、干净已提交内容、3e6b75e 基线；新文件要求缺失。额外运行文件、路径越界、子路径软链、源/目标漂移均拒绝 |
| 备份与排空 | 逐文件备份及哈希 manifest 在停止前落盘；停止活动生产者并排空后执行 SQLite backup/quick_check，再复验安装来源与目标哈希 |
| 首次激活 | activation_attempted 在首次自有 restart 之前写入 manifest；随后任何不确定启动结果均保留 v3 Python，以 gate0 加旧静态功能回滚 |
| 激活前恢复 | 全量还原前再次排空，并只读检查已接受批次协议。任意新协议批次存在均禁止旧 worker；代码未完整、畸形记录、查库失败或超出扫描预算则保持停止待核对 |
| 恢复失败 | 额外 app/备份漂移使常规恢复失败时，仅对固定 drop-in 尝试 gate0 与紧急排空，不恢复代码。只有 sidecar 实际 inactive 且无在途才记录 emergency_hold_completed；排空失败记录未完成，不复活生产者，不掩盖原异常 |
| 原运行状态 | 正常成功或功能回滚保留调用前 active/enabled 状态，不启用原停用 timer。只读核验进程实际 Excel flag 后才恢复生产者；紧急 hold 保持停止并明确失败状态 |
| 持久事实 | 所有恢复路径均不还原旧数据库、不删任务/短链；显式回滚始终保留兼容执行/对账代码，原 manifest 可重放 |

部署专项 24/24 通过，覆盖复制/重载/健康失败、激活前外部已接受 v3、残缺兼容代码、查库失败、备份漂移、紧急排空失败及漂移拒绝。另用独立隔离调用验证原 active/disabled/not-found timer 状态恢复，以及畸形 JSON、超过 10,000 条历史记录时禁止旧代码恢复。

后续上线须完成 GitHub 推送、服务器隔离验证、备份与排空、窄重启及只读验收。上述通过结论不替代这些发布步骤。
