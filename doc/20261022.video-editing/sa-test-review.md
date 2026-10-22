# SA 测试评审

## 结论

测试设计与独立实施审查已完成，已执行结果覆盖来源身份、权限、未知结果、版本冲突、逐剧编辑、真实COS与GPU制作。BUG-007候选Python126项由主实施、后端负责人、GPU实施者分别运行，均125通过、1项可选COS SDK跳过；runtime18项通过，包括慢COS HEAD不阻塞旧任务GET的并发测试。独立review通过无阻断；CPU核心f29f45ff和GPU核心1747c5cd已推送，GPU候选真实隔离恢复通过，生产部署与独立回读通过。BUG-006已部署117项及服务器service29项保留为历史证据。JS23项通过（模型9、浏览器事件/API契约14）；GPU实施者最终旧模块215项为209通过、6跳过，各子集不重复累计。

合成素材的两条独立时间线已在真实GPU出片并完成下载、完整解码、逐帧及音频区间检查，完成任务的独立监听进程停启恢复也已通过。正式Chrome双真实剧已分别完成12.0秒 / 360帧、9.7秒 / 291帧成片，授权下载、SHA / 大小 / COS身份、完整解码和成功scratch清理通过；两任务generation=1串行执行，旧23份GPU账本hash不变。浏览器自动上传及UI下载落盘回执仍未完成验收。

## 已验证的关键边界

- 共享ready素材可读、个人项目和任务隔离、原始来源身份及当前产品可用性校验；权限用例基于可信会话桩，多人实会话矩阵仍需另验。
- 多段删除、排序、重复素材、公共设置不覆盖逐剧剪切，30fps端点及0.3秒淡化扣时，短片段明确拒绝，保存竞态与同operation_id提交幂等。BUG-006新增9项覆盖并发、各任务状态、新版本、历史重复批次、操作冲突及管理员归属；已按 `9fc968a98924e658895240a8763162a2b97c207f` 部署。正式Chrome刷新重开v16再次制作后，原batch1 / task2 / output2、completed和generation=1不变，只新增operation映射1条。
- COS实际两分片、原会话续传、重复完成、过期签名续签、匿名403与CORS预检；CPU只接收元数据。
- GPU成片真实格式、配方帧序、contain / cover、淡化、静音、缓存、私有回执与成功临时目录清理。
- 隔离任务 `ve_4483dabb553b53369d18aff38f18bd6f` 完成后，停止并重启同一独立listener，再GET和重复POST命中cached；generation=1，COS ETag / LastModified / SHA与账本hash不变。正式GPU及发布服务PID、旧23份账本不变，CPU claim已恢复。
- 首次CPU分发sys缺失与Nginx路由缺失已修复，真实未登录JSON401确认新接口到达鉴权层；401不证明登录后的业务完成。

## 当前验收与剩余限制

正式Chrome已完成真实剧集探测、6/2秒裁剪、2/4秒切割并删除中间片段、整集移除后撤销和重排，选择系统验收共享素材后提交双剧并取得两条completed成片。冻结版本与逐剧配方核对通过，成片H.264 / AAC、720×1280、30fps，音视频时长差0，私有COS匿名HEAD403。浏览器素材可播，A / B均完整播放至ended，currentTime与duration分别为12秒 / 9.7秒，readyState=4且无media.error，证据为 `browser-finished-preview.json`。浏览器下载事件超时，Chrome下载页被浏览器安全策略禁止，UI落盘回执未确认；独立授权URL下载校验已通过，两者分别记账。

用户已登录但找不到扩展file URL访问开关，Chrome自动文件选择仍受阻，浏览器自动上传未验收。系统验收操作者通过标准生产服务创建上传、工作站直传COS及正常probe的链路已通过；该证据不能替代浏览器文件选择、浏览器直传和断线续传验收。

完成态隔离停启没有在下载或渲染中kill进程，也没有重启正式GPU服务。BUG-007候选仅在无上传意图、固定COS输出认证HEAD全404、原expiry绑定并经CAS创建新generation epoch时允许原ID恢复，上传未知仍阻挡重建。本地回归和独立review通过；真实隔离任务 `ve_0acb6a25ed8c5137aee7286f6df8a7a4` 在gen1强制disk_budget失败、0媒体 / 0上传后，模拟cleanup now+86401秒，再按原ID CAS恢复gen2并完成真实FFmpeg / 私有COS。重复resume、submit与独立runtime重启缓存无新增调用，匿名403、scratch清理、旧liveworker及全账本不变；隔离退出和GPU空闲已确认，生产部署与独立回读通过。

上述清理分支使用模拟时间，没有实际等待24小时，也没有kill运行中的下载 / 渲染。旧新任务同时排队压测、多人真实权限矩阵、8GiB实传仍未覆盖。真实隔离证据为 `output/video-editing-validation/bug007-preupload_expiry_20261022_a-results.json`；外部制作 / 盈量推送是本期预留项。

BUG-007随后已部署GPU1747c5cd并完成独立回读：全部29份账本hash不变、busy0 / 并发1 / 队列代码默认8，原正式两任务completed / gen1 / 原时间戳与scratch清理保持。CPU API未重启，旧claimer和剪辑worker恢复active，发布进程不变，GPU与隧道active、两端接单开启。`final-production-readback.json` 与 `compatible-gpu-update.json` 证明该生产闭环；不扩大为真实混合队列压测或浏览器自动上传 / 下载落盘回执通过。

详细计数、持久证据与当前边界见 `test-report.md`、`final-backend-review.md`；部署事实以 `deploy.md` 为准。
