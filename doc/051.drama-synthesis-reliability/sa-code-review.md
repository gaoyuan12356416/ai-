# 代码评审

状态：最终候选 `a1519413b23d20acab035853b0f5aeebee53e9ac`（tree `2bc83028916e6bc3a6cd7a4cd6cf5f8bc07735ec`）已推送/远端回读；frame/deadline修复后的两轮独立增量终审P0/P1/P2均为0。生产切换仍受最终候选Linux、真实API重启、媒体、COS、长片及排空门禁约束。候选推送不等于生产发布。

## 范围

异步 GPU runtime/HTTP handler、CPU 观察客户端与租约、原子结果和配方回填、下载/转码/成片检查点、drama异步上下文专用的持久COS分片、app 接入、两份生产页面与独立进度模块。未改其他平台发布、共享 FB 渲染器、数据库权限或生产服务。

## 本轮发现和处置

| 风险 | 修复/验证 |
| --- | --- |
| 封面回调断线导致 GPU 仍运行但 CPU 判失败 | 短超时回调移至原任务 GET 观察过程；网络失败只重连；完成结果不依赖再次发送封面 |
| 同一 CPU lease 内自动重试遗留旧观察线程 | 每次 process_job 独立停止事件，所有退出路径 stop + join 后才允许重试 |
| 显式恢复停在 processing_cover 导致无法及时 claim | 已有异步记录的重试置 queued，原 GPU 状态单独显示 |
| URL/HEAD 旧快速补账绕过 GPU manifest 校验 | 新异步记录只接受绑定当前输入指纹的v3 manifest；每个选中产物须由本地SHA及认证HEAD逐项核对，禁止文件名推导URL；旧同步任务保留原合同 |
| 同大小对象替换或manifest伪造导致复用错误产物 | v3记录bucket/key/SHA/size/ETag/binding，复用时重新认证HEAD；缺字段、指纹不符、远端元数据不符或本地变化均进入人工恢复，不重制覆盖 |
| 首次创建结果目录或replace后掉电丢失完成记录 | Linux按父目录fsync、文件fsync、replace、目录fsync、readback顺序提交；回填/通知不明保留成片和检查点，Windows仅验证逻辑原子性 |
| concat只比较codec/尺寸而误直拼 | 比较完整音视频流签名及extradata；标准化输出和检查点重放都重新probe，缺失或不一致显式失败 |
| 标准化仅改BT.709/逐行标签、不同几何或短/缺音轨仍拼接 | 冻结第1集偶数画布与源SAR，真实colorspace、等比scale+pad和显式parity bwdif；已有音轨apad、缺音轨补静音并以视频为界；源信息缺失失败关闭。源SHA/顺序/profile/完整plan绑定新旧入口checkpoint，fresh/replay均重新probe |
| 片头图片缺VUI时用auto猜色或为兼容静默跳过片头 | 当前只接受无ICC/Adobe覆盖的JFIF/sRGB合同，真实转换到BT.709 limited并写VUI；其他格式在FFmpeg前拒绝。必须用实际封面完成短样验收，若不满足则扩展受验证合同，不能删片头上线 |
| 失败通知标记阻止后续成功通知 | 首次非 done→done 原子事务清失败标记并保留历史时间；done 重放不重置；同步旧流程保持原语义 |
| 已知 GPU 执行丢记录后盲重提 | 已知 generation 后 GET 404 进入待核查，不提交新制作 |
| Popen 成功但 PID 记录写入失败形成孤儿 | 捕获启动/记录错误，杀本次子进程并 wait，再清记录；未知启动仍禁止自动恢复 |
| 模板原生进度 stage 别名未展示时长比例 | rendering_random 与 rendering 均使用实际 out_time/duration |
| 主任务待核查错误被最后远端运行阶段遮盖 | 页面优先展示主任务失败/待核查原因，连接断开保留真实最后进展 |
| 迟到封面回调覆盖已使用片头 | 首次绑定原子发布，重复同值不改时间，异值拒绝 |
| 成片已改名但最终检查点写失败，下次覆盖重渲染 | 启动先留start guard；验收后写绑定源/配方/产物的durable prepared，再改名并写完成记录。rename/final-save故障可校验复原，不启动编码器；只有guard或未登记旧输出则保留并停止，相关离线故障用例已覆盖 |
| 已验收成片的prepared记录本身写不下，清理时丢文件 | 保留临时成片和未完成guard，下一次不自动重制；只有同步制作明确失败或规格不合格时清理自己的未完成guard，不能把guard当完成记录 |
| 去BGM公开副本或上传响应丢失后再次执行Demucs；concat/no-BGM只有“视频可读”没有身份边界 | concat与去BGM都在publish前写本地完成记录，绑定冻结输入、上游产物或有序片段、处理profile及产物SHA/大小；恢复时先严格load记录。工作区成片有效则只恢复公开副本和续传，缺记录、损坏、冲突或落盘失败都保留并停止 |
| 固定10800秒把健康长片在3小时强制杀掉；验收systemd仍用43200秒又会先于动态预算结束 | drama独立 `DRAMA_GPU_RENDER_TIMEOUT` 为运维下限；冻结时长按0.10x、25%余量、1800秒收尾计算初始预算，取配置/12小时/计算值最大并封顶24小时。严格媒体时间/帧推进均刷新stall，只有正向媒体时间用于估算并延长deadline；0.5ms/1ms微增量有效，frame-only不猜测预算，相等/倒退/fps/speed无效；poll在判定前二次drain，默认1800秒无推进触发stall。验收render unit外层为90000秒，其他动作43200秒，benchmark保存configured/planned/global并由launcher重算核对。离线生产与验收路径已覆盖，不代表真机长片通过 |
| 已有out_time后frame-only复用陈旧媒体时间再次延长deadline；300秒资格阈值前pending泄漏到后续frame/空批次 | 分开计算out_time/frame严格推进，两者都刷新stall，只有本批次out_time推进创建deadline plan；pending在资格判断前消费。补齐“先out_time后frame-only”和“`t<300` out_time→阈值后frame-only→空批次”生产路径回归；旧实现与只拆分advanced的半修均被变异回放拒绝，修复后两轮独立增量终审P0/P1/P2为0 |
| 有界FFmpeg进度队列饱和或乱序包让页面/sidecar倒退 | queue满时fold出out_time/frame等推进高水位，并保留最新fps/speed；同stage/generation的out_time/frame/bytes/percent在 `AsyncRuntime.emit` 只取数值max，非推进指标可更新，stage切换沿用清空语义。真实maxsize=8写10包及持久emit测试通过 |
| timeout/信号/Popen失败缺私有诊断，或诊断泄露命令、URL和凭据 | 按job/generation原子写0600、64KiB受限sidecar；只保留安全码、阶段、预算/高水位、returncode/signal、stderr字节/SHA及静态标签。exit137、signal-9、native Popen含敏感异常与stderr精确字节均走生产路径验证，原文不落盘也不出现在公开错误 |
| 进程未停便clear、reader存活或final cleanup反转已提交成片 | `clear_process`仅stopped时删除持久身份，alive/unknown/probe异常失败关闭；reader未结束保留guard/partial。失败先写诊断并确认停止才清未验证输出；校验通过的partial先形成durable prepared再final commit，提交后的收尾异常不删除或反转结果 |
| 异步上传重试仅以公开HEAD长度判断，误用同key旧对象 | 仅drama异步上下文改用 `cos_upload.resume_upload`，认证HEAD必须同时匹配上传标识、SHA/大小元数据、Content-Length及完成记录；无可信检查点的现存对象不收编、不覆盖 |
| 分片/create/complete丢响应后新建上传或丢断点 | `.runtime/uploads`独立保存目标/源/UploadId/阶段；列举已有分片并与本地长度/MD5核对，只补缺失分片。create未知停止核查，不再create/abort；complete丢响应由绑定元数据的HEAD对账，真实COS验证仍是单独门禁 |
| COS SDK内部POST重试绕过持久化保护 | 真机SDK 1.9.44默认retry=3；仅异步专用客户端明确retry=0，旧调用保持SDK默认。新增真实SDK假transport用例，必须在目标SDK环境零skip通过 |
| Git2.27把`core.fsmonitor=false`当外部命令，且COS clean门禁的`git status`可执行候选filter | 两个固定Git wrapper统一使用跨版本空值覆盖，并在首次index读取前只允许唯一command-line空值；任何local/worktree/include fsmonitor值失败关闭。COS不再调用status/ignored/error-unmatch，改为精确HEAD tree、stage-0 index、`-v/-f` flags、流式操作系统全树和Python原生Git-blob SHA比较；固定MinGit2.27恶意marker、完整回归及socket硬拒绝通过，CPU/HK Git2.27精确checkout的445+13也已通过 |
| COS验收以dirty/replace checkout、index隐藏位、`.pth/.pyc`或影子HTTP栈伪造通过 | Git固定二进制和精确commit/tree，拒绝tracked工作树改动、staged改动、untracked、ignored及skip-worktree/assume-unchanged，禁replace与fsmonitor并核对关键blob；固定Python以`-I -S -B`启动，SDK及传输依赖在导入前递归验证root-owned只读树并拒绝symlink、`.pth/.pyc/.pyo/.egg-link`。ffprobe双管道运行期限流，任何中断均kill+wait；任一不明在读凭据前失败 |
| 最后一次HEAD后另一writer抢先写入同key | Complete带 `x-cos-forbid-overwrite:true`，创建和完成前只读检查桶版本控制，Enabled/Suspended/读取不明均停止。已只读确认当前桶未启用版本控制，未修改桶配置；新增竞争窗口及SDK请求头用例 |

上述 app 边界采用真实函数 AST 提取与 fake 依赖测试，无完整 app 导入副作用。CPU 可靠性代理再次只读复核已修并发/回填入口；GPU/媒体模块由各自作者运行专项并提交证据。最终候选本地受控SDK六模块478/478、FB16/16、页面25/25；最新frame/deadline增量完成两轮独立终审，P0/P1/P2均为0。COS新分片模块及最新增量的实际测试范围/计数以主任务 [测试报告](test-report.md) 为准，不把代码审查或离线替身当作Linux、真实FFmpeg、真实COS或生产成功。

## 仍未关闭的发布门禁

- 最终候选a151941的Linux真实进程身份/重启分支与生产运行解释器验证；历史dc0bad8的445+13不能替代，a151941预期466+13尚待新书面窗口。
- 下载 1/2/4（满足增长再 8）与 CPUQuota 200/400% 参数比较。
- 约 90 分钟真实媒体完整解码、音画/衔接/模板抽样及 RSS/线程曲线。
- 旧正式渲染 RSS 随时长增长，尚不能将短样通过视为长片通过。
- 隔离COS前缀中的实际分片/丢响应恢复、认证HEAD元数据可见性与上传完成对账；本地成片不可额外重渲染。
- 原任务已自然结束并核对为只有封面、无成片；保留输入、配方和CPU failed状态。当前仍须等并行迁移任务书面释放本轮新媒体窗口，并在执行前重新核查端口、目录、进程及资源；不能复用旧窗口或以候选代码代替协调许可。
- 本地通过后先推送精确GitHub候选到隔离目录，完成上述门禁才提升同一SHA到生产。步骤见 [媒体验收手册](media-acceptance-runbook.md) 与 [发布合同](deploy.md)。
