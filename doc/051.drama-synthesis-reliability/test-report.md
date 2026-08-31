# 测试报告（持续更新）

状态：已提交候选 `23a08e5f933fbbdbea3346b22c680a4180113380` 的本地及CPU/HK真实运行时回归通过；当前媒体launcher、COS验收驱动、运行代码和文档仍是**未提交增量**，尚未绑定新的候选SHA，也未部署生产。当前增量已改变运行文件，旧 `570c1bd` 的15文件manifest明确失效；必须在最终提交后重新生成并逐字节核对新清单。仓库根 `output/` 始终排除于stage/commit及交付清单，不得为clean门禁删除、移动或清空。以下阶段分别验收，不能互相代替。

## 已完成

- 2026-08-31冻结增量本地合并Python回归：**454/454通过、0跳过，25.252秒**。命令：`python -m unittest scripts.test_drama_synthesis_upgrade scripts.test_drama_synthesis_gpu_cache scripts.test_drama_synthesis_cpu_catalog scripts.test_drama_synthesis_remote_client scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_media_pipeline -q`；分项为83/110/16/30/66/149。使用受控COS SDK测试树，测试transport不访问COS。另跑FB prepare worker **16/16、1.606秒**；cache在 `socket.connect/connect_ex` 硬拒绝下仍 **110/110**。
- 两页面逻辑/JavaScript：25 项，OK；包括真实函数生成行、总耗时、UTC/北京时间、未知分母不造进度、XSS 转义、10 秒拉取与隐藏/权限门禁。
- 已覆盖超过四小时模拟、丢响应、队列/幂等/冲突、CPU 旧租约、完成原子事务及回滚、通知失败不重制、下载/续传 200/206/416/源变化/半文件、转码集序及兼容直拼、成片检查点、真实短子进程身份。
- 国内/国际 CDN：两组整文件 SHA 相同，三组 Range SHA 相同；性能方向随样本/缓存变化。见 [CDN 实测](cdn-evaluation.md)，不启用统一国际默认。
- 真实应用内浏览器完成组件检查：使用实际页面行函数、样式、运行模块及明确标注的模拟数据，没有模拟登录、API 或生产操作。下载、转码、模板、上传、连接恢复、未知分母六个状态显示正确；DOM 两次读取总耗时从 `4小时0分12秒` 增长为 `4小时1分27秒`。窄窗口按任务表横向滚动，状态文字不逐字竖排。见 [组件截图](evidence/ui-component-browser.png)。此项不等同真实应用认证/API 端到端验收。
- Python 3.9 AST及当前解释器compile均通过（18个本期Python文件）；48个文档相对链接无缺失，10份证据JSON可解析，28个候选代码/文档文件未命中私钥或常见云/API凭据格式。以上均是本地静态检查，不等同真实3.9/3.10运行时、真实凭据或网络验收。
- 第一候选 `cc50140b46a8d126e08a322364336722ac74fec1` 已从GitHub分别检出到CPU/HK独立目录，真实Python3.9.6/3.10.20各运行285项：均284通过、1失败、0跳过。唯一失败为测试把SDK实际传出的 `b'true'` 与字符串 `true` 直接比较；禁止覆盖头已经传递，现修正断言接受两种等价HTTP字节形式。失败日志保留，不改写为通过。
- 候选 `570c1bdaba7eddb9cf881a5df7efee976c2d6fb0` 已从GitHub取到两个全新独立目录：CPU Python3.9.6/COS SDK1.9.44 **287/287通过、0跳过，14.403秒**；HK Python3.10.20/COS SDK1.9.44 **287/287通过、0跳过，16.047秒**。包括真实SDK传输适配层的禁止覆盖头和丢创建响应只发一次POST用例；该层使用测试transport，不访问COS。未启动GPU制作，未改变生产服务。见 [目标运行时证据](evidence/linux-runtime-tests-570c1bd-20260828.json)。
- 原case已结束但没有成片：COS完整列表仅封面、随机视频HEAD404、GPU完成manifest缺失；退出时间与旧10800秒渲染限时精确吻合，但旧代码未保留原异常，原因标为推断，不写成已捕获TimeoutExpired。没有OOM/重启证据；输入与配方保留。见 [原任务核对证据](evidence/original-case-outcome-20260828.json)。未重制或改CPU任务状态。
- 真实CPU数据库的私有在线备份副本迁移通过：Python3.9.6/SQLite3.26.0，候选570c1bd；连续3次执行`ensure_runtime_table`，27张原表共24,670行的逐表数据哈希及原schema不变，仅增加runtime表及主键索引，完整性检查为ok。生产连接采用`mode=ro`、query_only及拒绝写入authorizer；生产库前后SHA/大小/inode/mtime/schema一致。原始副本仅保存在CPU私有0700目录，文件0600，不上传Git/COS，不操作生产API。见 [数据库副本证据](evidence/cpu-sqlite-copy-validation-20260828.json)。
- 历史23a08e5增量新增独立v1资源自检工具后，本地完整回归为**300项、298通过、2项因本机无COS SDK而跳过，14.749秒**；JavaScript25项通过。新增13项覆盖成员/目录身份、父级配额与余量、memsw读回、降权/权限残留、固定probe与错误拒绝。工具只执行8MiB/3秒普通进程，不接受任意命令，也不是媒体launcher。该历史计数不能替代上方冻结增量454/454。
- 资源自检候选23a08e5已完成双端GitHub精确检出及**各300/300通过、0跳过**：HK15.682秒，CPU14.599秒。CPU首次测试因测试驱动额外设置`PYTHONNOUSERSITE=1`而产生4个导入错误；保留失败日志，随后按实际服务环境重跑通过，没有安装依赖或更改服务。HK仅给测试账户增加本任务acceptance父目录的search ACL，使其可读取候选代码，未授予父目录listing、未改素材/数据库权限。见 [300项目标运行时证据](evidence/linux-runtime-tests-23a08e5-20260828.json)。
- HK首次独立256MiB资源自检按systemd `Nice=10`属性提交后，实际进程仍为Nice0；工具以`cpu_scheduling_mismatch`、exit78拒绝，未运行probe或媒体。失败证据原样保留，不能写成通过，见 [首次拒绝](evidence/resource-guard-probe-20260828.json)。随后改用固定 `/usr/bin/nice -n 10` 前缀：2核和4核两次正向自检通过，另一次故意用2核实际配额验证“期望4核”被拒绝；这是**两次成功加一次预期负向拒绝**，不是三次guard成功。实际Nice10、256MiB memory/memsw、swappiness0、Tasks128、降权身份和无额外能力均已读回，未启动媒体、未改生产，见 [原生nice自检](evidence/resource-guard-probe-native-nice-20260828.json)。本轮恢复审查只复核本地归档摘要，没有重新下载远端journal复算其哈希。
- 2026-08-31恢复前只读基线：HK剧集/FB进程未重启，无ffmpeg/ffprobe，GPU无计算进程，约30.16GiB可用内存；CPU `127.0.0.1:18788/healthz` 返回200且仍为旧生产代码，原case仍为failed。未写数据库、未启动媒体、未触网COS，见 [恢复基线](evidence/resume-baseline-20260831.json)。这些是时间点快照，媒体前必须重查。

## 尚待完成

| 验收 | 状态 |
| --- | --- |
| 最新增量的完整回归和候选 SHA 绑定 | 本地454/454、FB16/16、页面25/25已通过；独立终审及GitHub新SHA绑定待完成 |
| 真浏览器组件检查 | 通过；真实应用认证/API 端到端仍待验证 |
| CPU Python 3.9 / GPU Python 3.10 Linux 隔离回归 | 同一23a08e5各300/300通过、0跳过 |
| 真实CPU数据库在线副本上的增量schema及幂等验证 | 通过；3次调用、原表数据/schema不变，未对生产库迁移 |
| 新异步 API 真实短样故障/重启验证 | 待运行 |
| 下载 1/2/4/8 严格门禁与 CPU 参数对照 | 待运行 |
| 约 90 分钟真实媒体、完整解码与资源曲线 | 待运行 |
| 原任务结果回填核对 | 已核对本轮没有成片，不进行虚假完成回填；保留输入，未重制 |
| GitHub 精确版生产发布、回滚证据 | 未发布 |
| 用户视觉与业务验收 | 未提交人工验收 |

当前未新增服务器、未降低质量、未改生产并发/CPU 配额或其他平台发布行为；没有真实社交平台测试发布。

当前媒体窗口仍由迁移任务 `gpu-service-migration-20260828T1502` 协调：截至本报告更新时，该任务仍在执行且没有明确释放本轮新的窗口；不启动HK媒体渲染、下载压测或COS写入。CPU公共目录/主API和HK剧集部署须另行协调，不能与迁移切换重叠。单元测试通过不解除此门禁，也不关闭长片内存增长缺陷。
