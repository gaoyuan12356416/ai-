# 测试报告

## 测试结论

截至2026-10-22，BUG-007候选已实现失败scratch清理后的有条件恢复，本地功能Python126项（125通过、1项可选COS SDK跳过），独立review已通过、无阻断问题，真实隔离恢复通过，生产部署与独立回读通过。BUG-006已部署历史基线为Python117项（116通过、1跳过）；按 `9fc968a98924e658895240a8763162a2b97c207f` 窄范围部署后，服务器service29项及正式Chrome刷新重提回读通过。JS23项全部通过；旧模块最终215项为209通过、6跳过，GPU实施者已完成本轮复跑，各阶段不累加。

真实COS分片、续传、重复完成、过期续签、私有性及CORS预检已验证。合成素材的真实GPU出片、逐帧及音频区间检查，以及完成任务的独立listener停启恢复已通过。正式Chrome提交的双真实剧也已分别完成12.0秒和9.7秒成片，独立授权下载、完整解码、SHA / 大小 / COS身份与成功临时目录清理通过。两部成片均在浏览器完整播放到ended且无media.error；浏览器下载落盘回执与本地文件上传未完成。运行中断恢复及真实混合生产队列等剩余项仍须明确保留。

## 测试范围

共享素材库、项目多剧时间线、素材重复引用、剪切 / 删除 / 排序 / 撤销、公共参数套用、30fps时长、COS分片客户端与真实对象、自动保存与版本竞争、来源 / 产品权限、未知上传及任务恢复、批次版本成片账本、目录查询边界、任务状态及临时链接DTO；旧剧集合成相关回归由独立实施者执行。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 跳过 / 未完成 |
| --- | --- | --- | --- | --- |
| 本地功能 Python（含BUG-007修复） | 126 | 125 | 0 | 1：可选 COS SDK；独立review通过；真实隔离恢复通过，生产部署与独立回读通过 |
| JS 纯模型 / MD5 | 9 | 9 | 0 | 0 |
| JS 浏览器事件 / API 契约（模拟网络） | 14 | 14 | 0 | 0 |
| JS 语法检查 | 1 | 1 | 0 | 0 |
| 旧模块最终回归（BUG-007后复跑） | 215 | 209 | 0 | 6 |
| 本地浏览器逐剧隔离 / 删除 / 撤销 | 1 组 | 1 组 | 0 | 0 |
| 真实 COS 原会话上传 / 续传 / 完成 / 私有性 | 1 组 | 1 组 | 0 | 0 |
| 真实 COS 1秒签名过期与续签 | 1 组 | 1 组 | 0 | 0 |
| CORS 原策略保留及AI域名预检 | 1 组 | 1 组 | 0 | 0 |
| GPU真实媒体探测 | 3 | 3 | 0 | 0 |
| GPU真实组装出片（合成测试素材，两条独立时间线） | 2 | 2 | 0 | 0 |
| 成片下载 / 完整解码 / 逐帧及音频区间核验 | 2 | 2 | 0 | 0 |
| GPU重复缓存 / 私有COS回执 / 成功临时目录清理 | 1 组 | 1 组 | 0 | 0 |
| GPU切换后的独立health / 环境 / 历史账本回读 | 1 组 | 1 组 | 0 | helper首次检查异常，独立回读完成 |
| 完成态任务独立listener停启 / GET / 重复POST缓存 | 1 组 | 1 组 | 0 | 未执行运行中kill注入 |
| 正式Chrome提交双真实剧出片 / 授权下载校验 | 2 | 2 | 0 | 浏览器播放与下载回执单独记录 |
| 正式Chrome成片完整播放 | 2 | 2：A ended12秒、B ended9.7秒 | 0 | 0；两片media.error均为空 |
| 浏览器下载落盘回执 | 待验收 | — | — | 下载事件超时；Chrome下载页被浏览器安全策略禁止 |
| 浏览器文件选择 / 直传 / 断线续传 | 待验收 | — | — | 扩展未开启file URL访问，自动文件选择受阻 |
| 运行中断恢复 / 混合生产队列 | 待验收 | — | — | 未覆盖 |
| 同项目同版本跨刷新重提（BUG-006） | 新增9项，已计入Python总数；另1组线上 | 全部通过 | 0 | 原batch / task / output及generation不变，仅新增操作映射 |
| 失败临时目录清理后恢复（BUG-007） | 1组真实FFmpeg / COS隔离验收 | 1组 | 0 | 清理now模拟+86401秒；生产部署与独立回读通过 |

测试组数量不能与单元用例直接相加。Python126由主实施、后端负责人和GPU实施者分别独立执行，均为125通过、1项可选SDK跳过；runtime子集18项全部通过，已计入126。BUG-007新增9项恢复测试中的最后一项验证慢COS HEAD不持有共享runtime锁、不阻塞旧任务GET。117是BUG-006已部署历史基线，不另行叠加。GPU实施者最终复跑旧模块215项，209通过、6跳过；跳过不计为通过。独立后端子集41项、service子集29项及服务器29项复测均不另加。BUG-007独立review通过无阻断，CPU核心 `f29f45ff51946a2e5709137d91e7539f6e36041f` 和GPU核心 `1747c5cd560903032b90ce18d961bc1fe3caddb6` 已推送GitHub；GPU候选真实隔离恢复通过，生产窄更新和最终独立回读通过。主机及持久证据以 `deploy.md` 和验收记录为准。

## 缺陷情况

保存响应竞态、未知写结果重试、时长帧对齐、Unix秒时间显示、探测恢复、暂停接单兼容已修复。独立后端审查发现的恢复状态覆盖、完成崩溃悬停、旧来源产品门禁与runtime错误映射共4项均已复现修复，详见 `sa-code-review.md`。目录长扫见 `bugs/BUG-002.md`。SAR / contain / cover 的制作缺陷见 `bugs/BUG-001.md`；真实GPU的后续组装失败另由FFmpeg 7.1中setpts清除滤镜链帧率引起，见 `bugs/BUG-003.md`。两项制作缺陷已修复，并以最终GPU成片复验通过；不将这两项通过扩大为整个生产链路通过。

部署helper在systemctl启动返回后立即检查HTTP，遇到监听尚未就绪时进入异常路径，见 `bugs/BUG-004.md`。本地已补有限启动轮询和4项回归；线上当前版本由主实施独立回读确认，本次helper修复未再次执行线上部署。

首次CPU安装缺少局部sys导入与Nginx专用路由，已按 `c6d394ab` 修复并真实回读未登录JSON401，见 `bugs/BUG-005.md`。同版本跨刷新重提重复批次的BUG-006已修复、窄范围部署并通过正式Chrome重提及服务端回读。BUG-007修复前缺少清理后恢复路径；当前候选仅在无上传意图、固定COS输出认证HEAD明确404、expiry绑定配方及原generation时，以CAS沿同job建立新generation和scratch epoch。上传未知、对象存在或核实失败继续阻挡重建，真实隔离恢复已通过，生产部署与独立回读通过。

## 验证证据

执行位置：本功能独立工作树根目录，2026-10-22。

```powershell
node --check static/video-editing.js
node --test tests/test_video_editing_frontend.js tests/test_video_editing_browser_contract.js
```

结果：`tests 23 / pass 23 / fail 0 / skipped 0`。

- MD5用 Node crypto 做独立对照，长度覆盖0、55/56/57、63/64/65、127/128/129、16KiB和16MiB；不以复制算法作为验证。
- 计算两次切割后删除中间片段、0.3秒画音淡化扣时、短片拒绝、30fps一帧阈值、排序后末段cut、多剧公共套用不改变剧集保留区间及重复素材引用。
- 首次保存请求未返回时继续编辑再新建项目，先保存新编辑再切换；POST响应未知用相同op和冻结内容核实；PATCH响应未知读回匹配状态；真正版本冲突不覆盖远端、不丢本地草稿，用户可明确另存副本保留修改。
- multipart网络桩预置已上传第1片，确认恢复时只上传余片；一片403后重新签名成功；最多3并发；完成前核实成功数；没有创建新会话，也没有将临时签名或URL写入localStorage。
- 产品目录真实字段 `label` / `app` 保留可读名称与产品ID；空搜索、非数字单字在客户端拦截，不请求后端目录；至少两字前缀才发起查询。
- 新增的6项客户端契约测试覆盖实际GPU阶段 / dict进度、数字进度兼容、两剧完整编辑提交与成片链接刷新、原probe恢复跟踪，以及实际expires_at和离开项目后的迟到预览响应。dict只展示当前片段时间、传输量或步骤，不推算整体百分比。
- 主实施者用真实浏览器打开本地 `video-editing.html?demo=1`，已记录4片/50秒→3片/37.7秒、另一剧不变、撤销恢复的交互结果。

### 真实COS验证（主实施执行）

- 工作站17MiB文件直接发往COS，拆为16MiB与1MiB两片；CPU只接收创建 / 签名 / 核实元数据，不接收或落盘上传视频字节。
- 重新建立客户端会话后，针对原上传会话执行reconcile并续传；重复complete返回同一已完成结果。该证据是原会话恢复，不是新建COS UploadId重传。
- 已完成对象匿名读取返回403，确认私有对象控制生效。
- 使用1秒短签名，过期PUT返回403；对原会话和原分片续签后PUT返回200，并在同一会话完成合并。
- 保留桶内原3条CORS规则，追加AI后台域名规则；对应OPTIONS预检返回200。已有规则未整表覆盖。

上述真实传输由本地直传验证程序完成，证明COS与服务端签名链路；不代替正式登录页面中从文件选择到最终预览的整条浏览器操作验收。网络桩的403重签用例与该真实过期签名验证分别记账。

后续使用明确标注的“系统验收”操作者，经标准生产 `uploads.create/sign`、工作站直接COS PUT、`complete_upload` 与正常worker probe创建了共享ready素材“验收素材（合成图案，勿投放）.mp4”。621866字节、6秒、60fps的本机文件与GPU SHA256一致，匿名HEAD403；证据为 `output/video-editing-validation/standard-acceptance-upload.json`。没有创建真人会话或直写业务SQL。这是标准服务层直传成功，不能替代因扩展file URL权限受阻的Chrome文件选择 / 浏览器上传验收。

### GPU与旧模块验证

- 验证候选为 `ab27b932c272c63766ffc4d930f22291b5248e8e`。在香港GPU执行3个probe及两条独立时间线的完整render，输入为带帧号 / 时间戳的合成测试素材：25fps竖屏有声、24fps横屏无声、60fps方屏有声。覆盖素材重复插入、源片段跳切、contain / cover、硬切、连续淡化及静音。它证明真实GPU制作链路，不代替正式业务短剧和登录页面验收。
- 两条成片分别为108帧 / 3.6秒和102帧 / 3.4秒，均为MP4、H.264、720×1280、30fps、yuv420p、SAR 1:1，音频为AAC / 48kHz / 双声道，音视频时长一致。通过私有COS短期授权下载，字节数与SHA256均匹配回执，完整解码无错误；短期签名没有写入验收文件。
- 独立检查按冻结配方从源片生成预期帧，以180×320 RGB比较每一帧，覆盖来源顺序、保留区间和适配几何。两条成片的全片平均绝对误差为0.254和0.261（像素范围0—255），单帧最大平均误差为0.909和0.797。四处淡化均为9帧 / 0.3秒；中点混合帧误差为0.616—0.827，明显低于仅使用任一侧画面的误差27.649—48.898。
- 横屏contain片段上下黑边像素均值为0，cover片段边缘有有效画面；两张抽帧联系表已人工复核。原声区间RMS约0.088，淡化区间有信号，无声源补静音及显式静音区间RMS为0，后续未静音片段恢复原声。该证据确认测试素材的帧序、音视频时长与声音区间，不将合成音调验证写成人声口型同步验收。
- 3个probe和2个render均通过私有性检查；相同冻结请求重复读取返回相同已完成结果，命中缓存；成功任务在COS回执确认后移除工作目录，持久结果与回执保留。独立验收只读取产物，没有重启服务或启动额外远程渲染。
- GPU实施者提供旧模块回归统计215项、209通过、6跳过、0失败；它是代码回归证据，不等于真实混合生产队列已完成全部验收。

本地证据位于独立工作树的 `output/video-editing-validation/`：`gpu-canary-results.json` 保存冻结配方、产物回执和缓存 / 私有性 / 清理结果；`finished-media-validation.json` 保存下载SHA256、FFprobe、完整解码和逐帧 / RMS数据；`finished-0-contact.png` 与 `finished-1-contact.png` 保存抽帧复核图。GPU端对应汇总为 `/data/drama-synthesis-gpu/results/video-editing-canary/acceptance.json`。这些是本次验收产物，不是已提交的生产代码。

Python总量与旧模块回归使用以下命令；实际GPU成片和下载检查单独记账，不计入这两个用例总量：

```powershell
python -B -m unittest discover -s tests -p 'test_video_editing_*.py' -q
python -B -m unittest scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_gpu_cache scripts.test_drama_synthesis_remote_client -q
```

### GPU部署启动与独立回读

主实施已将GPU切换至 `81f75eca25bd472cddec21ccbd2e09b5f728fe51`，候选预检通过。`systemctl start` 返回时HTTP尚未监听，helper立即health检查失败，异常路径的媒体守卫返回 `media_listener_unverified`，保留新reader，没有盲目停止未知状态的进程。主实施随后独立回读确认health正常、release / profile / 并发1 / 环境符合预期，23份历史账本SHA256未变，媒体执行为空闲；回读没有追加重启。证据为 `/data/drama-synthesis-gpu/deploy-backups/video-editing-20261022T050848Z/result-reconciled.json`。

因此记为“部署发生启动检查竞态，后经独立回读确认有效状态”，不记为helper全程无异常成功。新helper仅在连接拒绝时最多等待60秒，版本 / 设置不匹配、HTTP错误、超时等立即失败；本地helper测试14项通过。该修复供后续部署使用，未改变已验证的线上运行payload。历史账本回读与下述独立完成态恢复是两份不同证据。

### 完成态隔离重启恢复

隔离任务 `ve_4483dabb553b53369d18aff38f18bd6f` 完成后，使用独立runtime / 目录和监听 `127.0.0.1:18798` 保存快照，核实进程身份后停止并重启该listener；随后GET仍为completed，重复POST命中cached。job_id、generation=1、completed_at、配方 / 结果摘要、COS ETag / LastModified / SHA、账本及结果manifest hash均不变，`output_recreated=false`。正式GPU及发布服务PID、旧23份记录未变化，隔离进程已退出，CPU claim恢复原active状态。

本项是“完成→独立listener停启→GET→重复POST”的实际恢复，既不是运行中kill下载 / 渲染进程，也不是重启正式GPU服务。证据为本地 `output/video-editing-validation/isolated-recovery-results.json` 及GPU `/data/drama-synthesis-gpu/validation/recovery/completed_restart_20261022_a/` 的before / after / replay / acceptance文件；具体操作和边界见 `final-backend-review.md`。

### BUG-007清理后原ID恢复的真实隔离验收

候选 `1747c5cd560903032b90ce18d961bc1fe3caddb6` 的任务 `ve_0acb6a25ed8c5137aee7286f6df8a7a4` 在generation1强制disk_budget失败，该阶段0媒体执行、0上传。确认停止后调用cleanup时模拟now+86401秒，再以原ID和原generation CAS恢复为generation2，真实执行FFmpeg并上传私有COS；原expiry未改，scratch epoch绑定generation2及expiry摘要。**模拟时间用于触发24小时清理分支，没有实际等待24小时。**

最终1.7秒H.264 / AAC、720×1280、30fps成片为778617字节。调用计数download1 / ffmpeg3 / create multipart1 / upload part1 / complete1 / abort0；重复resume、submit和独立runtime重启后均命中cached，没有新增调用。匿名HEAD403、scratch清理通过，旧liveworker及全部live账本不变，发布进程不变；隔离进程停止，GPU计算空闲，safe_to_restore_claim=true。该隔离快照内CPU协调器仍暂停，随后生产更新完成后的恢复状态见下一节。

证据为 `output/video-editing-validation/bug007-preupload_expiry_20261022_a-results.json` 及GPU `/data/drama-synthesis-gpu/validation/bug007/preupload_expiry_20261022_a`。该项证明候选在模拟到期后的真实恢复，不是运行中kill恢复；生产部署使用下一节的独立回读证据。

### BUG-007生产更新最终回读

GPU已部署 `1747c5cd560903032b90ce18d961bc1fe3caddb6`，备份 `/data/drama-synthesis-gpu/deploy-backups/compatible-update-20261022T061934Z-1747c5cd5609`。全部29份live账本hash不变，busy=0、制作并发1、队列容量沿代码默认8，GPU PID1560374与隧道active。原两条正式任务保持completed、generation=1及原完成时间，scratch不存在；此29份是本次更新基线，区别于早期验收核对的23份旧账本。

CPU API PID3770172在本次GPU更新中未重启；旧claimer PID3797297和剪辑worker PID3797300已恢复active，发布进程2937249 / 2576490不变，CPU与GPU接单均开启。BUG-007生产部署及独立回读通过，本缺陷关闭。

本地证据为 `output/video-editing-validation/final-production-readback.json` 和 `compatible-gpu-update.json`；CPU证据 `/mnt/data-disk/video-editing/validation/final-production-readback.json`，GPU证据为上述备份目录的 `final-readback.json`。并发配置与账本保持不等于已执行真实旧新混合队列压测。

### 正式Chrome双剧成片与线上幂等验收

已完成真实剧集ready后添加、6/2秒裁剪、2/4秒切割删除中间内容、整集删除后撤销、重排及选择系统验收共享素材。正式双剧项目版本16的草稿、冻结项目版本和逐任务配方一致，公共设置没有覆盖裁剪点。两条任务现均completed、generation=1，且按同一制作槽串行执行；对应成片ready，batch / version / output稳定ID与冻结配方不变。

| 正式成片 | 帧数 / 时长 | 大小 | 媒体与对象核验 |
| --- | --- | --- | --- |
| A：1wwAOKT9Tz | 360帧 / 12.0秒 | 6,405,219字节 | H.264 / AAC、720×1280、30fps、SAR1:1、48kHz双声道；音视频时长差0 |
| B：uWmMdxk9BE | 291帧 / 9.7秒 | 5,395,571字节 | 同规格，音视频时长差0 |

两片均通过授权私有URL下载、SHA256 / 字节数 / 认证HEAD一致性、完整解码和匿名HEAD403检查；成功scratch已删除，旧23份GPU账本hash不变。证据为 `output/video-editing-validation/formal-output-acceptance-summary.json`、`production-finished-media-validation.json`、`formal-output-receipts.json`。该数据确认格式、时长与文件完整性，不等同于逐处人工人声口型判断。

BUG-006按 `9fc968a98924e658895240a8763162a2b97c207f` 仅部署service / store两个文件并重启API，服务器service29项通过，worker与发布进程PID不变。正式Chrome刷新、重新打开项目v16再点击制作后，服务端仍为1个原batch、2个原task和2个原output；任务completed及generation=1不变，仅新增1个operation映射。CPU证据为 `/mnt/data-disk/video-editing/validation/browser-reload-idempotence.json`。

浏览器素材可播放；A和B成片均完整播放至ended，currentTime分别等于duration12秒和9.7秒，paused=true、readyState=4、media.error为空。A曾缓冲较慢，最终已完成播放。浏览器下载事件等待超时，Chrome下载页受浏览器安全策略禁止，不能声称UI落盘回执通过；上述独立授权下载校验不替代该浏览器回执。最终播放与下载观察保存在 `output/video-editing-validation/browser-finished-preview.json`。用户已登录，但尚找不到扩展file URL访问开关，浏览器自动本地文件上传仍未验收。

## 遗留风险

- COS传输、匿名拒绝与CORS预检已有实测；用户已登录但找不到扩展file URL访问开关，Chrome自动文件选择受阻，浏览器直传 / 中断续传仍待验收。标准服务层直传不替代浏览器操作，实际大文件速率不在本次统计中。
- `?demo=1` 不含真实媒体；浏览器预览交叉淡化用于定位。正式双剧完整解码、授权下载校验及浏览器完整播放均通过；浏览器下载落盘回执待补，不将音视频时长相同等同于逐处人声口型同步判断。
- 完成态隔离停启和缓存回读已通过；下载 / 渲染中断后的同ID恢复、正式GPU服务中断与旧新任务同时排队的调度压测未由该证据覆盖。
- BUG-007候选已实现有条件恢复：无上传意图、固定COS输出认证HEAD404、绑定expiry及新generation epoch，并通过原generation CAS后才可沿原ID恢复；上传结果未知仍须先核实。独立review已通过，真实隔离恢复、生产部署和独立回读均通过，本缺陷已关闭。
- 普通有权限 / 无模块权限用户与管理员的多人实会话矩阵、8GiB文件上限实传仍需独立证据。失败清理后的真实FFmpeg / COS恢复已用模拟now+86401秒验证，未实际等待自然24小时。
- 项目和任务列表当前各返回最新100条，无翻页参数；素材库支持分页。
- 真实多人同时编辑发生冲突时必须保留本地草稿，不能以测试里的同内容回读逻辑覆盖他人不同内容。

## 发布建议

正式双剧独立出片、浏览器完整播放、私有COS下载与文件核验、成功临时目录清理、完成态隔离恢复及BUG-006线上刷新重提已通过。BUG-007候选本地126项回归通过，独立review及旧模块最终回归已通过，真实隔离恢复已通过，生产部署与独立回读已完成；浏览器文件选择 / 直传 / 中断续传、浏览器下载落盘回执、多人实会话权限、运行中断恢复、旧新混合队列与8GiB实传保留为明确限制。仅因文档补充不重做已通过检查，后续实现变化须执行相关回归。


## BUG-008 交互响应最终验收（2026-10-22）

`node --test tests/test_video_editing_frontend.js tests/test_video_editing_browser_contract.js`：51项全部通过（42浏览器事件合约、9模型），0跳过。实施、测试与独立审查分别复跑；不是累加为153项。JS语法、差异检查及技能要求的8文件Python编译通过。

正式修复前账本第三集从创建到ready约23秒，GPU执行14.424秒；四轮80次来源读取中76次读未选且无校验任务的剧集。此等待含真实媒体下载/校验，并非SQL耗时；不承诺修改页面后真实GPU校验瞬间完成。

冻结版本真实Chrome连接loopback延迟fixture：产品/搜索/剧集读取各延迟2秒、probe回执延迟3秒，第三集18秒后才ready。点击后立即看到“已就绪2/3”、忙态和取消等待；无需第二次点击，最终自动出现3片/30秒并保存v1。记录仅1次probe POST，来源GET全部指向第三集，未查询已ready的前两集和未选来源。详情 `output/video-editing-responsive-validation/browser-fixture-summary.json`、`fixture-events.json` 及 `local-waiting-final.png` / `local-auto-added-final.png`。这是慢请求交互验收，不是真实GPU吞吐基准。

独立审核另验证了A项目载入期间切B失败仍可重试、A的旧轮询错误不污染B等待。51项覆盖关闭/ESC/取消、连续点击、目录乱序、批量部分4xx/坏2xx、unknown提交、共享task403/404、owner恢复原ID、失败排空单飞、项目源加载锁定和保存不被列表刷新拖住。服务端/上传/GPU实现本次未改，既有未验收的真实大文件和故障注入边界仍按前文保留。
