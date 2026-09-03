# 测试报告（持续更新）

状态：最终运行候选 `a1519413b23d20acab035853b0f5aeebee53e9ac`、tree `2bc83028916e6bc3a6cd7a4cd6cf5f8bc07735ec` 已提交、推送GitHub并从远端ref回读；本地受控SDK完整回归及frame/deadline修复后两轮独立增量终审通过。**新候选尚未取得CPU/HK Linux无媒体书面窗口，也未进行真实媒体、COS、真实API重启或生产部署。** 历史候选 `dc0bad87b24318a09fc886c34d6957cbee8f4720` 的双端Linux结果继续保留，不能冒充a151941结果；旧候选 `1367dd4e508dc81f0fb7eb9c128ed47a22b67d06` 因CPU Git 2.27回归失败作废，失败保持原样记录。相对420957b的17项code/static运行变更overlay及4个验收工具哈希已从a151941的Git blob生成。仓库根 `output/` 始终排除于stage/commit及交付清单，不得为clean门禁删除、移动或清空。以下阶段分别验收，不能互相代替。

## 已完成

- 2026-08-31最终候选本地受控SDK合并Python回归：**478/478通过、0跳过，26.653秒**。命令：`python -m unittest scripts.test_drama_synthesis_upgrade scripts.test_drama_synthesis_gpu_cache scripts.test_drama_synthesis_cpu_catalog scripts.test_drama_synthesis_remote_client scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_media_pipeline -q`；分项为85/111/16/30/68/168。测试transport不访问COS。另跑FB prepare worker **16/16**，页面JavaScript **25/25**。
- 候选83aec118在文档/合同终审中发现1个P1：已有正向out_time后，后续frame-only会复用陈旧媒体时间再次规划deadline；若out_time出现在300秒资格阈值前，pending还可能泄漏到后续frame/空批次。最终候选a151941已分离out_time/frame推进并在资格判断前消费本批次pending，补齐两种混合序列变异回归；修复后两轮独立增量终审均为 **P0=0、P1=0、P2=0**。既有测试有效性复核还包括真实queue饱和fold、严格微增量、poll-time二次drain、reader存活、stopped-only `clear_process`、final cleanup、诊断sidecar隐私、returncode/signal和原生Popen启动失败脱敏；Python3.9/3.10 grammar解析通过。
- 历史候选dc0bad8的目标Linux无媒体回归已通过：CPU Python3.9.6、HK Python3.10.20均从新的root私有目录运行相同严格runner；原始清单main457/FB16，按冻结ID只排除12+3个真实loopback，CPU **445/445＋13/13**，HK **445/445＋13/13**，两端均0失败、0错误、0跳过。CPU fresh GitHub checkout与HK bundle detached checkout分别逐一核对1260个tree/index/blob和权限；bundle在CPU、本机中继、HK的SHA256一致。HK首次因独立CPython未包含service site-packages而在收集前拒绝，0项执行且失败证据保留；run2只加入冻结只读依赖目录，未安装或修改依赖。四个剧集/FB生产PID、startticks及restart count前后不变，验收进程归零。见 [双端无媒体证据](evidence/linux-no-media-regression-dc0bad8-20260831.json)。该结果不等于最终候选a151941通过；a151941按同一冻结排除12+3后预期为主套件466项、FB13项，须取得新的书面窗口后执行，当前不得写PASS。
- 固定Git for Windows MinGit **2.27.0.windows.1** 下cache **111/111通过、0跳过，28.088秒**；恶意fsmonitor及clean/process filter均未执行。两个Git wrapper使用旧版安全空值覆盖并在读取index前拒绝额外local/worktree/include fsmonitor配置；COS clean gate不再调用可能执行filter的`git status`，改为HEAD tree、stage-0 index、`-v/-f` flags、实际文件树及原生Git-blob SHA精确比较。此项仍不能替代目标Linux发行版回归。
- 最终运行候选提交 `a1519413b23d20acab035853b0f5aeebee53e9ac` 已推送GitHub并从 `origin/codex/drama-synthesis-reliability-20260828` 回读，tree为 `2bc83028916e6bc3a6cd7a4cd6cf5f8bc07735ec`；后续文档/证据提交不能把分支头SHA冒充运行候选。最终候选的新严格零socket清单为全量478+FB16，按冻结ID排除12+3个真实loopback后应运行466+13；此处只记录预期清单，尚无新Linux执行证据。
- [运行文件清单](evidence/runtime-file-manifest-a151941.json) 从最终候选Git blob生成：相对420957b完整列出17项code/static运行变更overlay，另列4个验收工具；相对23a08e5的运行增量为10项，相对dc0bad8仅 `features/drama_synthesis/gpu.py` 与 `features/drama_synthesis/async_runtime.py` 两项变化。相对直接父候选83aec118，运行overlay只变化 `features/drama_synthesis/gpu.py`，4个验收工具不变；配套测试变化按清单范围明确排除。该overlay不是独立部署包，也未穷举全部未变依赖；部署必须使用完整精确Git checkout。清单不包含 `output/`、私有SDK/配方/媒体、测试、数据库、Token或生产配置，也不表示这些文件已经部署。历史 [dc0bad8清单](evidence/runtime-file-manifest-dc0bad8.json) 只用于解释既有Linux证据。
- 作废候选1367dd4已在CPU新鲜GitHub checkout完成严格无媒体回归：候选/tree/1258个tracked blob及Python3.9 AST门禁通过，但过滤后主套件442项出现1 error、1 skip；FB 13/13通过。错误来自POSIX下测试假Git路径并暴露跨平台测试缺陷，skip来自错误的Git>=2.36门禁；进一步审计确认旧代码会把`core.fsmonitor=false`当作Git2.27 hook路径。HK未开始测试，bundle中继随该SHA取消；未启动媒体、COS或服务。见 [失败证据](evidence/linux-no-media-regression-1367dd4-20260831.json)。
- 两页面逻辑/JavaScript：25 项，OK；包括真实函数生成行、总耗时、UTC/北京时间、未知分母不造进度、XSS 转义、10 秒拉取与隐藏/权限门禁。
- 已覆盖超过四小时模拟、丢响应、队列/幂等/冲突、CPU 旧租约、完成原子事务及回滚、通知失败不重制、下载/续传 200/206/416/源变化/半文件、转码集序及兼容直拼、成片检查点、真实短子进程身份。
- 失败取证与最终清理已在候选代码及本地测试闭环：模板timeout、stall、非零/信号退出和原生Popen启动失败写每job/generation的私有0600、原子、64KiB受限诊断sidecar；只保存安全码、阶段、预算/进度高水位、returncode/signal、stderr字节数/SHA256和静态标签，不保存stderr原文、命令、路径、URL、异常原文或凭据。`clear_process`仅确认stopped才清持久身份，reader存活或进程alive/unknown时保留guard/partial；校验通过的partial先形成durable prepared再提交，提交成功后的收尾异常不反转结果。上述为本地实现/测试结论，尚未做最终候选Linux进程、真实FFmpeg故障或重启后sidecar审计。
- 媒体验收外层不再固定12小时截断动态预算：render unit为90000秒，prepare/decode/guard仍为43200秒；launcher精确接受systemd等价回读 `1d 1h` 并拒绝动作间互换。benchmark evidence保存配置下限、按probe时长计算的planned budget和86400秒内部硬上限，launcher重新计算后才接受；90分钟fixture的planned为69300秒。standalone失败fallback与生产AsyncRuntime诊断路径及字段边界已分开记录。上述只证明验收工具合同，本轮仍未启动真实unit或媒体。
- 国内/国际 CDN：两组整文件 SHA 相同，三组 Range SHA 相同；性能方向随样本/缓存变化。见 [CDN 实测](cdn-evaluation.md)，不启用统一国际默认。
- 真实应用内浏览器完成组件检查：使用实际页面行函数、样式、运行模块及明确标注的模拟数据，没有模拟登录、API 或生产操作。下载、转码、模板、上传、连接恢复、未知分母六个状态显示正确；DOM 两次读取总耗时从 `4小时0分12秒` 增长为 `4小时1分27秒`。窄窗口按任务表横向滚动，状态文字不逐字竖排。见 [组件截图](evidence/ui-component-browser.png)。此项不等同真实应用认证/API 端到端验收。
- Python 3.9 AST及当前解释器compile均通过（18个本期Python文件）；全部文档相对链接存在且大小写匹配，全部证据JSON可解析，候选与本期文档的定向凭据扫描未发现真实私钥或云/API凭据。测试fixture中的显式假值不冒充真实凭据问题。以上静态检查不代替目标运行时、真实凭据或网络验收；目标3.9/3.10无媒体运行结果由上方独立证据记录。
- 第一候选 `cc50140b46a8d126e08a322364336722ac74fec1` 已从GitHub分别检出到CPU/HK独立目录，真实Python3.9.6/3.10.20各运行285项：均284通过、1失败、0跳过。唯一失败为测试把SDK实际传出的 `b'true'` 与字符串 `true` 直接比较；禁止覆盖头已经传递，现修正断言接受两种等价HTTP字节形式。失败日志保留，不改写为通过。
- 候选 `570c1bdaba7eddb9cf881a5df7efee976c2d6fb0` 已从GitHub取到两个全新独立目录：CPU Python3.9.6/COS SDK1.9.44 **287/287通过、0跳过，14.403秒**；HK Python3.10.20/COS SDK1.9.44 **287/287通过、0跳过，16.047秒**。包括真实SDK传输适配层的禁止覆盖头和丢创建响应只发一次POST用例；该层使用测试transport，不访问COS。未启动GPU制作，未改变生产服务。见 [目标运行时证据](evidence/linux-runtime-tests-570c1bd-20260828.json)。
- 原case已结束但没有成片：COS完整列表仅封面、随机视频HEAD404、GPU完成manifest缺失；退出时间与旧10800秒渲染限时精确吻合，但旧代码未保留原异常，原因标为推断，不写成已捕获TimeoutExpired。没有OOM/重启证据；输入与配方保留。见 [原任务核对证据](evidence/original-case-outcome-20260828.json)。未重制或改CPU任务状态。
- 真实CPU数据库的私有在线备份副本迁移通过：Python3.9.6/SQLite3.26.0，候选570c1bd；连续3次执行`ensure_runtime_table`，27张原表共24,670行的逐表数据哈希及原schema不变，仅增加runtime表及主键索引，完整性检查为ok。生产连接采用`mode=ro`、query_only及拒绝写入authorizer；生产库前后SHA/大小/inode/mtime/schema一致。原始副本仅保存在CPU私有0700目录，文件0600，不上传Git/COS，不操作生产API。见 [数据库副本证据](evidence/cpu-sqlite-copy-validation-20260828.json)。
- 历史23a08e5增量新增独立v1资源自检工具后，本地完整回归为**300项、298通过、2项因本机无COS SDK而跳过，14.749秒**；JavaScript25项通过。新增13项覆盖成员/目录身份、父级配额与余量、memsw读回、降权/权限残留、固定probe与错误拒绝。工具只执行8MiB/3秒普通进程，不接受任意命令，也不是媒体launcher。该历史计数不能替代上方最终候选478/478。
- 资源自检候选23a08e5已完成双端GitHub精确检出及**各300/300通过、0跳过**：HK15.682秒，CPU14.599秒。CPU首次测试因测试驱动额外设置`PYTHONNOUSERSITE=1`而产生4个导入错误；保留失败日志，随后按实际服务环境重跑通过，没有安装依赖或更改服务。HK仅给测试账户增加本任务acceptance父目录的search ACL，使其可读取候选代码，未授予父目录listing、未改素材/数据库权限。见 [300项目标运行时证据](evidence/linux-runtime-tests-23a08e5-20260828.json)。
- HK首次独立256MiB资源自检按systemd `Nice=10`属性提交后，实际进程仍为Nice0；工具以`cpu_scheduling_mismatch`、exit78拒绝，未运行probe或媒体。失败证据原样保留，不能写成通过，见 [首次拒绝](evidence/resource-guard-probe-20260828.json)。随后改用固定 `/usr/bin/nice -n 10` 前缀：2核和4核两次正向自检通过，另一次故意用2核实际配额验证“期望4核”被拒绝；这是**两次成功加一次预期负向拒绝**，不是三次guard成功。实际Nice10、256MiB memory/memsw、swappiness0、Tasks128、降权身份和无额外能力均已读回，未启动媒体、未改生产，见 [原生nice自检](evidence/resource-guard-probe-native-nice-20260828.json)。本轮恢复审查只复核本地归档摘要，没有重新下载远端journal复算其哈希。
- 2026-08-31恢复前只读基线：HK剧集/FB进程未重启，无ffmpeg/ffprobe，GPU无计算进程，约30.16GiB可用内存；CPU `127.0.0.1:18788/healthz` 返回200且仍为旧生产代码，原case仍为failed。未写数据库、未启动媒体、未触网COS，见 [恢复基线](evidence/resume-baseline-20260831.json)。这些是时间点快照，媒体前必须重查。

## 尚待完成

| 验收 | 状态 |
| --- | --- |
| 最新增量的完整回归和候选 SHA 绑定 | 最终候选a151941/tree 2bc8302已推送并远端回读；本地478/478、FB16/16、页面25/25；frame/deadline修复后两轮独立增量终审P0/P1/P2均为0 |
| 真浏览器组件检查 | 通过；真实应用认证/API 端到端仍待验证 |
| CPU Python 3.9 / GPU Python 3.10 Linux 隔离回归 | dc0bad8历史双端445+13通过；最终候选a151941预期排除12+3后466+13，尚待新书面窗口，未运行且不得写PASS |
| 真实CPU数据库在线副本上的增量schema及幂等验证 | 通过；3次调用、原表数据/schema不变，未对生产库迁移 |
| 新异步 API 真实短样故障/重启验证 | 待运行 |
| 私有失败诊断（failed stage、returncode/signal、stderr字节/SHA）及重启后审计 | 0600原子受限sidecar及公开脱敏已实现并通过本地测试；最终候选Linux、真实FFmpeg与重启后审计待运行 |
| 下载 1/2/4/8 严格门禁与 CPU 参数对照 | 待运行 |
| 约 90 分钟真实媒体、完整解码与资源曲线 | 待运行 |
| 原任务结果回填核对 | 已核对本轮没有成片，不进行虚假完成回填；保留输入，未重制 |
| GitHub 精确版生产发布、回滚证据 | 候选已推送供隔离验收；生产未部署、未重启API/worker，尚无生产回滚执行证据 |
| 用户视觉与业务验收 | 未提交人工验收 |

当前未新增服务器、未降低质量、未改生产并发/CPU 配额或其他平台发布行为；没有真实社交平台测试发布。

当前远端窗口仍由迁移任务 `gpu-service-migration-20260828T1502` 协调：dc0bad8的CPU/HK无媒体隔离许可已经执行完毕并关闭，**最终候选a151941的Linux无媒体、媒体/COS及部署均须取得新的书面窗口，不能沿用历史许可**；不启动HK媒体渲染、下载压测、COS写入、真实API重启或生产切换。CPU公共目录/主API和HK剧集部署须另行协调，不能与迁移切换重叠。历史无媒体回归通过不解除此门禁，也不关闭长片内存增长缺陷。
