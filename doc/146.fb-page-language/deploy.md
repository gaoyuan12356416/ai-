# 部署文档

## 当前状态

2026-09-18：主任务已部署语言实现 `4201910` 至 CPU。公开 HTML/JS 返回 HTTP 200 且哈希与发布文件一致，sidecar `/health` 返回 200；部署前后原有 1511 条任务、2245 条发布尝试、1205 条发布账本均未变化。QA 子任务未操作服务器或真实发布；本节线上证据由主任务提供。

最新主任务证据：最终应用版本 `90faff9` 已上线，192 项全套通过（23.191 秒）；HTTP 素材兼容修复 `f7cb58f` 已上线，TL 已重新取得 500 候选。GPU CPUShares 调至 4096，4 核 CPUQuota / 8 GiB / 2 jobs 上限保持不变。

正式 URL 的未登录浏览器 DOM 读回：`select#language` 与 `input#language` 合计 0 个，帮助文字明确按 Page 语言筛选，脚本版本含 `page-language-20260918`。未执行登录或保存交互。

容量前置版本 `e3bef98` 已部署 CPU/GPU；完整 FB 回归 186 项、随机排重 8 项通过，性能样本输出哈希一致且加速比 1.169。后续容量版本 `284f06e` 已进入 GitHub 并部署：基于 10 组真实发布样本（每组 4 条，22.93–57.72 秒），publisher 改为 8 路持续取任务、30 分钟领取窗口/预算、service 60 分钟；全局 Graph 0.5 秒调用间隔不变，预制作 lookahead 为 2 天，GPU 2 并发、CPUQuota 400%、MemoryMax 8G 保持不变。新增容量 4 项测试通过，0.111 秒。

## 变更与配置

增加 Page 语言路由并移除模板语言 UI；无需新增环境变量。既有表结构不变，未来运行扩充按需创建审计回执表。静态 HTML/JS 与 FB 自动发布服务需使用同一已验证版本。

## 部署与验收步骤

1. 完成适用 FB 全套回归、Python 编译和前端验证，记录 GitHub 提交。
2. 读取当前服务、计划器和执行租约；按项目既有发布流程备份配置和 SQLite，切换已验证版本。
3. 验证模板页面无语言控件、保存请求省略语言、旧模板仍可加载；读回 Page 池语言分布及异常数。
4. 如需扩充现有未来运行，先固定运行、模板版本、计划时间与缺失 Page 集，留存预览指纹和原任务快照；只追加授权范围的 Page。
5. 扩充后核对旧任务 ID、素材、制作结果、短链及发布账本不变；新增任务语言与 Page 语言一致；重复执行不增加重复任务。
6. 核对实际计划、制作和发布结果；区分 planned、ready、published、skipped、failed 与 unknown。不得把本地测试或健康接口当作平台成功凭证。

## 本次精确执行约束

主任务的操作脚本为 `scripts/fb_extend_future_pool.py`：先在数据库副本预演，固定任务绑定指纹，备份真实数据库，再执行追加与读回。2026-09-18 北京时间 17:19:48，主任务确认 `119..124` 共 6 个运行各有 145 个唯一 Page，已新增 `122 × 6 = 732` 条任务；原 1511 tasks / 2245 attempts / 1205 ledger 全部逐行对比不变。

2026-09-18 新增 122 个 Page 的发布时间为北京时间 21:30；原 23 个 Page 维持 18:54；2026-09-19 的既有 5 个时隙不改。五个业务 timer 已恢复 active，制作任务 1512/1513 已自然进入 preparing；这表示生产制作已启动，不表示已发布完成。

## CLI 必需维护窗口

此 CLI 不能在自动写入不中断的线上状态下运行。任务绑定和历史事实比对发生在扩充事务提交后，因此必须从副本预演、指纹核验、备份、apply 到最终读回全过程隔离并发写入；否则可能先提交变化，再触发事后断言。

主任务确认维护期内 scheduler、plan、prepare、runner、reconcile 五个 timer 持续停止，sidecar 重启前 `preparing/running/submitted/unknown` 均为 0，且没有其他启用的自动模板。已在 apply 与读回完成后恢复五个 timer 为 active，未提前恢复写入。

语言候选由唯一审计进程生成。最终 7 种语言各 500 条均已审核，cache 已 `0440` 冻结，SHA 为 `f6ee45c6e294eae76a493ff06e83dd753ae36e23b91d05dde4afc71801c12b1c`。预演与 apply 必须使用相同冻结候选及 metadata；脚本本身的缓存时间和配置校验不能替代内容哈希与维护窗口。副本预演确认旧 1511 条任务全行未改，计划新增 `6 × 122 = 732`。

## 候选准备快照

主任务已取回的候选数量：`en=5000`、`pl=1461`、`es=500`、`id=500`、`th=500`、`zh-tw=500`。后续 TL 原完整扫描返回 0，已定位为 BUG-002：自有 COS 历史 HTTP URL 被旧校验过滤；五个同对象 HTTPS HEAD 均为 200/video/mp4，描述唯一非空且剧已上线，不能视为缺视频或描述。

BUG-002 已随 `f7cb58f` 部署，TL 重建为 500 条。最终用于追加的 en、pl、es、id、th、zh-tw、tl 均为 500 条已审核候选，覆盖上文较早的准备快照；快照数受保留上限影响，不是素材全库总量。

保留生产 HTML 较新的 `quick-nav.js?v=20260915retired` 资源版本，不恢复已退役模块。

## 回滚

切回发布前代码与静态资源版本，按既有服务流程恢复。保留当前 SQLite 账本、已冻结任务及平台结果，不用旧数据库覆盖新增事实。任何取消未来任务或重放历史都必须有独立明确范围。

## 服务器替代巡检

用户明确关闭每 30 分钟的 Codex 自动任务。主任务已将 heartbeat `dramawave-145-page` 设为 `PAUSED` 并再次 view 确认；不重新启用该自动任务。

替代实现为 `scripts/fb_page_readiness_watch.py` 与 `fb-page-145-readiness.service/.timer`。它使用 SQLite `mode=ro` 和 `query_only=ON`，只读准确运行 `119..124`、145 Page 范围、冻结语言、已发布账本状态、服务健康及 timer 状态；不发布、补发或改业务数据。

server timer 在 2026-09-18/19 每 5 分钟运行，9 月 20 日 00:30:45 收尾，三条 OnCalendar 均显式 `Asia/Shanghai`。主任务已用 CPU systemd 239 验证日历，读回 17:25:45、9 月 19 日 00:00:45、9 月 20 日 00:30:45；QA 未安装或启动 unit。服务限制为 CPUQuota 20%、MemoryMax 256M、30 秒超时、Nice 10；`RequiresMountsFor=/mnt/data-disk` 与脚本 `os.path.ismount` 双重要求数据盘已挂载，避免写入根盘同名目录。

结果位于 `/mnt/data-disk/fb-auto-post-publisher/health/145-page/latest.json` 和 `observations.jsonl`，观察日志超过 4 MiB 轮换保留一份 previous。巡检的 `healthy` 表示当前未发现列出的异常，不能等同于已发布完成；`submitted` 单列，只有 `published` 计入计划日发布覆盖。制作 ETA 累计较早时隙工作量，并按具体输入时长除以基线 261.257874 秒缩放；未提供时长时沿用基线估值。

新增独立测试 `python -m unittest scripts.test_fb_readiness_watch -v`：17/17，通过 0.014 秒。超过计划时间 2 小时仍 running/submitted 会记为 `publication_confirmation_overdue`，仅报告、不重试。今天新增 122 条所选视频平均时长为 307.04 秒，按基线与 1.169 加速比推算约需 3.892 小时，相对 21:30 约有 17 分钟模型余量；这不是实测完成时间或按时承诺。巡检 `20eb3a8` 已部署并启用，首次报告及 17:35:46 快照均正常，精确证据见 production-verification.md。

17:23 主任务观察到自然 ready 3、preparing 2、新增错误 0；GPU 约占 4 核、1.5 GiB RAM、1.4 GiB VRAM，属于当时进度快照，不是整批完成证据。

## 最终证据与验证边界

- 语言实现 `4201910` 与容量调整 `284f06e` 的部署、HTTP/健康及原历史不变证据已记录；具体备份路径见 production-verification.md。
- BUG-002 `f7cb58f` 已部署、TL 500、最终 7 种语言各 500、冻结 cache SHA 及 192 项全套证据已记录。
- `119..124` 的 732 条追加已于 17:19:48 验证完成，6 个运行各 145 个唯一 Page、原三表逐行不变；完整审计文件路径和指纹已记入 production-verification.md。
- 登录后模板保存交互：未执行。
- 17:35:46 今日实际 published 覆盖 20 个 Page；未来 870 条中 ready 151、preparing 2、planned 717。过去 12 条 skipped 不重放；未来平台结果尚未到期，服务器持续记录。
- 服务器只读巡检已 enabled/active，首次报告正常，三个部署文件哈希一致；五个业务 timer active。自然成片 1512/1513/1514 的公网 HEAD、大小、SHA256、profile 均通过。
