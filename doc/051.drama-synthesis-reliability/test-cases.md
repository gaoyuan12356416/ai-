# 测试用例与验收矩阵

更新：2026-08-31。覆盖 [requirements.md](requirements.md) 的R1～R5。状态中的“本地通过”仅指仓库内隔离测试，不包含真实Linux重启、业务发布、真实COS写入或人工媒体验收。

## 测试数据与已执行检查

- 本地使用临时目录、临时SQLite、合成job_id、示例域名和假HTTP/媒体对象；真实短子进程仅执行Python空操作，无视频制作、无COS和业务平台调用。
- Linux后续验收必须使用独立work/result/public目录、测试job_id、私有冻结参数和隔离COS前缀；不得使用正式样例任务做重启、删除或重做试验。
- 媒体样本固定源文件SHA、配方SHA、素材manifest和编码参数。短样0.5～300秒，长样5400～7200秒；5秒样例不能充当90分钟验收。
- 下载测速固定1～8个资源，单资源最多32 MiB，单次最多256 MiB；私有URL文件不入Git，公开证据不含URL查询参数或Token。

冻结增量实际执行六模块合并回归 **454/454通过、0跳过，25.252秒**：upgrade83、cache110、CPU catalog16、CPU客户端30、GPU runtime66、media149；另跑FB prepare worker **16/16**。`node scripts/test_drama_synthesis_list_actions.js` 返回 **25/25、2页、0浏览器调用、0网络调用**；`node --check static/drama-job-runtime.js` 通过。cache在 `socket.connect/connect_ex` 硬拒绝下仍110/110。完整候选与历史证据以 [test-report.md](test-report.md) 为准；这些结果不代替目标Linux、真实COS或长片验收。

## 自动化及隔离故障用例

| 编号 | 场景与步骤 | 预期结果 | 级别 | 当前证据/状态 |
| --- | --- | --- | --- | --- |
| TC-01 | 新任务提交；在返回前读取运行JSON | 已原子持久化，返回202；记录不在可删除的单任务目录 | P0 | runtime本地通过 |
| TC-02 | 16个同job/同输入并发提交；再改源/线路/固定封面；等待任务保持原await标志回调封面 | 只执行一次，代次一致；输入冲突409，非法线路400；等待封面保持身份，固定封面不可改绑，旧无线路payload身份不变 | P0 | runtime本地通过；追加线路/封面回归通过 |
| TC-03 | 提交后丢弃HTTP响应，再GET/重复POST | 查询原执行，无第二次渲染；若从未确认且权威404，仍只同输入重提 | P0 | HTTP/runtime/client本地通过 |
| TC-04 | 模拟时间超过14400秒且仍有GPU任务；模拟短暂断连 | 不因总等待时间失败或重提；保留首次开始和真实进展，显示连接恢复 | P0 | runtime/client本地通过；不是实际四小时压测 |
| TC-05 | 制作槽被占用时查同任务、查完成缓存及回调封面 | 查询/缓存/封面不抢槽，不发生封面等待死锁 | P0 | runtime/HTTP本地通过 |
| TC-06 | 队列满、无Token、非法job_id/内容ID、非JSON值 | 新任务受限；已存在任务仍可读；无路径穿越和输入进入渲染 | P0 | runtime/HTTP本地通过 |
| TC-07 | 恢复queued记录；恢复已写完成manifest但丢响应的running记录 | 前者只启动一次；后者复用结果，不重渲染，不改首次开始 | P0 | runtime/cache本地通过 |
| TC-08 | 旧子进程存活/未知、PID复用、不同boot、Popen后未记录PID | 只有可靠停止证据可恢复；未知状态阻止重制，不按心跳年龄放行 | P0 | 身份/启动窗口fixture本地通过；真实Linux进程组演练待验 |
| TC-09 | `.runtime/jobs/locks` 首建目录fsync失败、账本文件fsync/原子替换失败、损坏账本、第二runtime实例抢owner锁、停机中抢锁 | 不返回虚假的202，不启动仅在内存的任务；持久目录在接单前完成，保留记录和owner | P0 | 本地故障注入通过；Linux真实双进程/磁盘/权限/掉电场景待验 |
| TC-10 | 已完成后旧代次发进度/失败；CPU旧worker_id或attempt回写 | 完成状态、产物和完成时间不被覆盖；旧worker不释放新租约 | P0 | runtime/client/SQLite本地通过 |
| TC-11 | 删除任务后迟到结果；配方提交失败后事务回滚 | 不重新创建任务；任务完成与配方一起回滚 | P0 | CPU runtime本地通过 |
| TC-12 | 完成结果重复消费；通知抛错后worker重入 | 保留第一次完成时间和通知标记；不重制媒体 | P0 | CPU worker/runtime本地通过；真实通知链路待隔离验收 |
| TC-13 | failed普通POST；显式resume；丢失resume响应并重放旧expected_generation | 普通POST不重启；显式恢复只增加一代；迟到重放不能再次增加代次 | P0 | GPU/CPU本地通过；实际用户重试链路待浏览器/API验收 |
| TC-14 | 新下载、旧非空文件、完整下载重启复用 | 仅完整长度/SHA/身份匹配才复用；旧非空文件不冒充成功 | P0 | media本地通过 |
| TC-15 | 断线后强ETag续传；弱/缺ETag；Range被200忽略 | 强validator才续传，其他完整重下；不把200新响应追加到旧内容 | P0 | media本地通过 |
| TC-16 | 源ETag变化、错位206/Content-Range、可证与不可证416 | 源变化停止；无效分段拒绝；416必须有完整证据，否则验证完整GET | P0 | media本地通过 |
| TC-17 | 下载截断/超长/编码响应/无长度、损坏已落盘前缀、崩溃残留尾部 | 不提升半文件；仅在已验证持久前缀后处理残留；错误不泄露地址 | P0 | media本地通过 |
| TC-18 | 表面codec/尺寸相同但profile、level、pix_fmt、几何/SAR、色彩、场序、rate/time_base、音轨长短/缺失、声道或H.264/AAC extradata不同；源信息缺失；源/profile/顺序/plan变化；片头为合规JFIF或PNG/ICC/Adobe JPEG；下载乱序完成 | 只有完整流签名一致才直拼；不完整/不同启动单标准化执行器。第1集偶数画布上等比scale+pad，真实BT.709 limited转换和显式bwdif；短音轨apad、无音轨补静音且不截视频。fresh/replay重新probe并匹配目标签名；checkpoint变化拒绝复用。合规JFIF片头真实转换，其他格式在FFmpeg前拒绝；始终保留片头和集序 | P0 | media离线因果fixture通过；真实FFmpeg、实际封面、短片和长片衔接待验 |
| TC-19 | concat、去BGM或模板成片已完成但上传失败/公开副本丢失；恢复时concat、Demucs和模板runner全部设为禁止调用；另造缺失/损坏/冲突记录及检查点写失败 | 有效工作区成片和本地完成记录复用，只恢复公开副本并续传；任何publish前已经持久提交记录；已有成片缺记录、源/配方/产物/身份冲突或记录写失败都保留并停止，不重制覆盖或上传 | P0 | concat/no-BGM/模板离线回归通过；真实COS上传恢复待验 |
| TC-20 | FFmpeg进度含NaN/非法值；正常退出与超时退出 | 使用真实媒体时间；先kill/wait确认退出再clear；无命令/URL泄露 | P0 | media/runtime本地通过；真实Linux FFmpeg待验 |

## 性能、页面及上线验收用例

| 编号 | 场景与步骤 | 通过条件 | 级别 | 当前状态 |
| --- | --- | --- | --- | --- |
| TC-21 | 固定源短样分别2核/2线程、4核/2线程、4核/4线程；新目录避免缓存命中 | 实际渲染器启动，冻结输入不变，记录耗时、RSS、线程、CPU节流和GPU占用 | P1 | 工具策略本地通过；实测待验 |
| TC-22 | 通过短样的组合运行约90分钟固定长片，完整解码并抽查首尾/集边界/动画 | 无解码错误，视频流/容器与源时长差维持既有0.15秒约束，音画/模板人工核验合格，资源无持续异常增长 | P0 | 待验，不能用短样替代 |
| TC-23 | 同资源按1/2/4路下载；有增长才测8路 | 8路对4路至少15%收益且错误率不增加；无收益维持4路 | P1 | 工具预算/4路基线约束本地通过；吞吐结论待验 |
| TC-24 | 相同并发/资源/样本比较img与accelerate候选；核对内容与响应 | 实测更快且无内容/校验退化才启用；抽样一致只标抽样证据，完整对象需另证 | P0 | 比较/冻结/隔离回退单测通过；[香港样本](cdn-evaluation.md)结果分化，维持original默认 |
| TC-25 | 两个真实页面观察下载/标准化/模板/上传、封面先完成、断连、跨小时耗时 | 中文阶段准确；仅阶段百分比；没有源地址/内部错误；封面不覆盖主阶段，耗时持续增长 | P0 | 25/25静态及组件浏览器检查通过；真实认证/API和用户视觉待验 |
| TC-26 | 原任务自然完成并对账后按GPU→CPU切换；在隔离环境排演回滚 | GitHub精确SHA、备份可用；DB/账本/manifest/成片保留；无重复正式制作或平台发布 | P0 | 待执行，生产未切换 |
| TC-27 | 固定16GiB launcher依次做无媒体guard-only、短样prepare、两轮三配置render及逐片decode；注入candidate worktree/local config/filter/ignored/index/权限变化、site/.pth、长源同大小替换、提交响应未知、BaseException及动作后cgroup计数增长 | 每个动作以 `-I -S -B` 使用固定unit/路径和同一精确候选；全HEAD blob、root-owned只读树、批次长源SHA、decode前后成片身份、提交intent/不重放、任意中断kill+wait及前后failcnt/memsw/swap/OOM均闭环；不能靠旧手工systemd模板绕过 | P0 | media149/149、upgrade83/83、FB16/16本地通过；真实Linux权限、16GiB unit与媒体均未运行 |
| TC-28 | 专用四键凭据、全新私有前缀和非敏感MP4执行真实COS分片；在Part1及Complete成功响应后各丢一次响应；测试通知、双匿名HEAD、owner-only ACL、3600+30期限；注入ignored/replace/fsmonitor、skip-worktree/assume-unchanged、Git环境、候选blob差异、qcloud/requests/urllib3/certifi shadow、`.pth/.pyc/.pyo/.egg-link`、symlink、ffprobe stdout/stderr超限及本地子进程中断 | 同一UploadId续传；完成重放零写；完整认证GET SHA一致；Create/Complete前通知为空，匿名两次403且只有Owner FULL_CONTROL；固定`python -I -S -B`下候选/传输/runtime任一不明都在读凭据或发COS请求前拒绝；超限/中断子进程kill+wait；无delete/abort/业务API | P0 | 最终验收驱动门禁41/41、全cache110/110，socket连接硬拒绝下仍110/110；真实COS写入待独立窗口，不能用mock代替 |
| TC-29 | 新v3 manifest缺/改输入指纹；同大小对象替换；改SHA、ETag、binding、bucket/key或远端元数据；异步执行无manifest但存在可预测公共文件名 | 每个选中产物均由本地SHA和认证HEAD匹配v3；任何差异进入 `recovery_required`，不得按文件名补URL、缓存未命中或重新渲染 | P0 | cache/app离线fixture及最终全量回归通过；真实COS HEAD待窗口 |
| TC-30 | 首次创建结果目录、临时文件fsync/replace/父目录fsync/readback各阶段故障；旧同步入口与专用worker争owner.lock；无COS配置 | 任何不确定均保留本地成片/检查点且阻止新渲染；旧同步与worker全局互斥；无COS只写旧兼容manifest并保留本地产物 | P0 | fault-order/锁离线fixture及最终全量回归通过；Linux掉电边界待隔离验收 |

## 回归与证据要求

保留既有输出选项、随机配方身份、片头、标准化、图层、素材目录、短链和YouTube对话框修复；既有FB/X/TT/YouTube发布流程不得发生真实测试发布。基础升级与CPU目录测试另跑，不能只跑新增用例。

每个隔离运行留存：候选SHA、基线、环境配额、冻结输入哈希、命令（无敏感值）、结果JSON、进程曲线、媒体校验和人工观察。任何失败必须记录真实缺陷；阻塞/未执行不写PASS。最终人工确认保留给用户。
