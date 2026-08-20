# 003.fb-page-auto-post 需求与技术设计

## 背景

运营需要在 AI 后台维护 Facebook Page 自动视频发布模板：选择 Page 池、配置与 X 自动发布一致的短剧/素材指标筛选、发布文案和固定/随机频率，系统在每个时隙为当时可发布的每个 Page 冻结一条任务。

只读盘点：35 个有效组（26 Post、9 AD），4,243 条成员关系、4,002 个唯一 Page；1,157 个 Page 有 `status=0` 且非空 Token，2,845 个缺少可用 Token。每个 Page 可能有 1–8 个 Token。旧表 `ads_facebook_post_publish_queue` 仍可能按 `execute_switch=1` 运行。

## 目标

- 管理模板、Page 池和运行记录，展示 Page 总数/可发布/缺 Token/跳过原因。
- 每个时隙冻结模板版本、Page 并随机选择视频；素材按 Page 冷却，跨 Page 可复用。
- Token 仅执行/对账时实时读取，随机且不放回；发布明确拒绝或对账明确凭证/权限失败才换 Token。
- unknown、已返回 Graph 对象 ID、处理中的视频绝不自动重发。
- 默认关闭真实 Meta 门禁；本需求不部署生产、不发真实帖子。

## 范围

### 包含

- 独立 `127.0.0.1:18835` sidecar；加法式 SQLite 模板版本、随机计划、运行、Page 快照、任务、尝试审计和发布事实。
- `kunlunads_dev` Page/Token/素材和 `ads_setting` 黑名单只读查询，无 MySQL DDL/DML。
- Feishu Cookie + `fb_page_posts` 权限；普通用户按邮箱唯一映射 `admin_user_group.sub_user_id`，只看 `g.user_id` 相同的组；管理员可看全部。
- 有效组为 `g.is_delete=0 AND g.type IN (0,1)`，仅包含 `type=0` Post 和 `type=1` AD；未知未来类型不误标为 AD。
- `ads_custom_source.type=2` 视频；素材源由服务端受控产品映射冻结。首发仅 `app_id=1479 / product=Dramawave / data_source=6 / metric_product=Dramawave / platform=0`，未知映射禁止启用，绝不回退 2。
- `POST /{page_id}/videos` 提交和 `GET /{object_id}?fields=id,status` 对账。
- 发布文案支持 `{{desc}}` 和 `{{url}}`：`{{desc}}` 来自同 app/content/language 的 `ads_drama_resource.desc`；`{{url}}` 为任务 ID 对应的不可变 `https://gy.g2flow.com/s2l/fb/{task_id}.html`，不得展开为素材 URL 或 W2A 长链。
- `{{url}}` 的 W2A 目标固定为 `https://www.dramawavew2a.com/ads/0/2049/view`，归因字段和 campaign 拼接规则沿用 TT，`af_channel` 固定为大小写精确的 `AIpost`。
- Graph 返回 ID 只进入 `submitted`；仅 ready/published 后进入 `published`。
- 固定/随机北京时间；随机计划按版本+日期持久化，以可行解计数 DP 抽样，不取整点，相邻至少 60 分钟；完整日窗口 24 次必须可生成。
- Graph execute/reconcile 每轮固定最多 4 个并发任务，每任务最多 8 个 Token；单 Graph 请求 120 秒、任务租约 1,200 秒、runner HTTP 1,300 秒、unit 1,500 秒。prepare 按 GPU 实际串行合同每轮只领取 1 个任务。

### 不包含

- 图片、文字、Reels、多附件；首版只支持视频 URL。
- 跨产品素材：首版所选组必须属于同一 `app_id/product`，素材产品由服务端派生。旧系统允许目标 Page 与素材产品不同，这是 V1 限制。
- 除本文固定 W2A 短链外的任意 URL 类型或自定义跳转目标；只允许模板宏 `{{drama_name}}`、`{{material_name}}`、`{{content_id}}`、`{{desc}}`、`{{url}}`，其中 `{{url}}` 最多一次。
- 旧队列写入/迁移/停用；Token 修改；生产发布或上线。

## 用户故事 / 业务规则

1. 普通用户只选自己负责的组；映射缺失/歧义闭锁。管理员可选全部。
2. API/UI 展示组名、类型、目标 app/product、总 Page、可发布、缺 Token。
3. 启用/每次运行前都按当前组成员检查旧队列、其他启用新版模板的 Page-ID 交集、空组和零可发布 Page；成员关系在启用后漂移也不得产生跨模板双队列。
4. 单模板多组按 Page ID 合并并保留全部 `group_ids`；未来不同时隙允许同 Page 的 planned/preparing/ready 任务提前并存，同一发布时间仍唯一；发布领取显式跳过同 Page 的 `running/submitted/unknown`，其中 `unknown` 形成永久人工闭锁。
5. 一个运行只执行一次有界候选查询；不得按 Page 重扫指标表。
5A. 同 Page 的 `planned/preparing/ready/running/submitted/unknown` 素材始终视为预留；`published/failed_without_retry` 在冷却窗口内占用。最终冷却重查、候选改选和 task 插入必须处于同一 `BEGIN IMMEDIATE`，并发 future planner 不得预留同一素材；首选被占用时改选冻结候选中的下一条。
6. 同模板上一个已到发布时间的任务仍 `planned/preparing/ready/running` 时返回中文积压原因；尚未到期的未来时隙可提前冻结和制作。
7. 无 Token 保存 `fb_page_missing_eligible_token`；无视频保存 `fb_auto_no_eligible_video`；冲突保存 `fb_auto_page_task_conflict`。
8. Graph 发布非 2xx 且无 ID 才是明确失败；断连/超时/非法或 2xx 缺 ID 是终态 unknown；任何返回 ID 都禁止重发。对账遇当前 Token 的明确凭证/权限失败时随机不放回尝试其余健康 Token；处理失败进入 `failed_without_retry`；全部 Token 明确无法对账进入保留 Graph ID 的终态 `unknown/attention`；网络或无法判定仍为 `submitted`，只允许稍后 GET 对账。
8A. 最坏 8 Token 路径按 `8×120秒+开销` 预算；execute/reconcile 在 1,200 秒租约内完成，第8个 Token 约968秒返回成功 ID 时仍须原子落账，不能被 stale cleanup 或并发对账抢占。
9. 尝试审计仅保存序号、Token 行 ID、`fb_user_id`、安全错误码/trace ID；不保存 Token 或哈希。
10. live gate 关闭时 tick 只清理过期 running，不读取 due 模板或创建 queued run。
11. enabled 模板禁止直接编辑；必须先停用、修改、再通过完整冲突检查启用。
12. Scheduler 进程内 single-flight；每分钟只在 SQLite 中把未来 `FB_AUTO_PREPARE_AHEAD_SECONDS=14400` 窗口内的时隙幂等写入 `due_slot`，持久 watermark、有界 catch-up，并记录过旧 missed。Page/素材冻结由独立 plan worker 执行，GPU 由 prepare worker 提前制作，Graph runner 仅领取达到 `planned_publish_at_utc` 的 ready 任务。
13. 素材目录以 `ads_custom_source s FORCE INDEX(PRIMARY)` 做完整有界 keyset 扫描，短剧资格用 `EXISTS ... FORCE INDEX(ac)` 去重；元数据再按 content 确定性取唯一行。不得在本地指标筛选/排序前任意截断 5,000 行；整体扫描有 600 秒/100 页 fail-closed 边界。
14. 对账更新 ledger 时保留发布阶段累计的 `definite_attempts`，不可归零。
15. `{{desc}}` 仅在模板使用时按素材页的 content ID 批量读取，不得逐 Page/逐素材 N+1 查询；空或同一身份多值的描述不得被选作该模板候选。描述压缩连续空白并最多冻结 4,096 字符。
16. `{{url}}` 在 task 自增 ID 取得后，于同一个 `BEGIN IMMEDIATE` 事务内冻结 short URL、W2A long URL 和最终 `message_text`；页面名缺失时使用 Page ID，素材 tag 缺失时使用 `FBauto`。后续模板、Page 名、素材名或 tag 变化不得改写历史任务。
17. Graph POST 前必须先将冻结 long URL 原子写到独立公开根 `/mnt/data-disk/fb-auto-post-public/s2l/fb`，形成不可变短链 wrapper；写入失败、目标冲突、路径越界或软链接目录均在 Graph 前失败，不调用任何 Page Token。Nginx 只允许 GET 精确的 `/s2l/fb/[1-9][0-9]{0,18}.html`，无目录索引并返回 no-store/security headers；公开目录与 SQLite 私有目录物理分离。

## 交互与流程

模板页加载权限、组及汇总；保存时派生产品并创建不可变版本。时隙触发冻结 Page 联集和一次候选快照，逐 Page 应用冷却。Runner 原子领取并提交 Graph；返回 ID 进入 submitted。Reconcile 只读查询状态，ready/published 才完成。

## 技术设计

### 影响模块

`features/fb_auto_posts/`、`scripts/fb_auto_post_*`、`app.py`、`.env.example`、导航、两张 HTML、`deploy/fb-auto-post-*`、`deploy/nginx-fb-auto-short-domain-location.conf`。

### 数据结构

运行库 SQLite 表：`fb_auto_template`、`fb_auto_template_version`、`fb_auto_schedule_plan`、`fb_auto_due_slot`、`fb_auto_run`、`fb_auto_run_page`、`fb_auto_task`、`fb_auto_publish_attempt`、`fb_auto_publish_ledger`。`fb_auto_task.short_url/long_url` 与 `message_text` 一起冻结。指标缓存使用独立 `FB_AUTO_METRIC_DB_PATH`；启动时与 `FB_AUTO_POST_DB_PATH` 同路径（含已存在文件的 samefile/symlink）立即失败，避免长指标写事务阻塞 scheduler/claim。迁移只建表/索引或补列。

### API / 接口

主 API 精确代理 `/api/admin/fb-auto-publish/{groups,templates,runs}`；`run-now` 只以 `operation_id` 幂等写入 manual due-slot 并返回 202，高成本冻结由 plan worker 异步完成。内部路由为 `/internal/fb-auto-post/{tick,plan-next,prepare-next,execute-next,reconcile-next}`；健康为 `GET /health`。

### 异常与边界

- Token、口令和 bearer 不进入 API/DOM/日志/SQLite/文档。
- 旧队列冲突只返回 queue id/name/status，不返回条件载荷。
- 候选查询固定 SELECT、源表 id keyset、单页 1,000、最多 100 页/整体 600 秒；每页 SQL 45 秒 hint。SQL 先去重并冻结短剧资格，完整目录进入本地 READY 指标筛选与两级排序。
- `unknown` 是人工关注终态，不重发；`submitted` 只允许 GET 对账重试，绝不再次 POST。

## 验收标准

- 真实字段为 `g.user_id`；黑名单默认 `ads_setting`。
- Page/权限/冲突/计数、单次素材读、计划、租约、failover、unknown、submitted/reconcile 有测试。
- `py_compile`、相关专项与 X/TT 合并回归、JS/JSON、diff、敏感扫描通过。
- `FB_AUTO_POST_LIVE_ENABLED=0`，无 Meta 写请求。

## 已确认决策与上线风险

已确认：频率作用于池内每个健康 Token Page；单模板同一 app/product；首发仅 Dramawave 受控映射；视频制作模板严格必填且暂时只有 `random_overlay`。模板语言冻结数据库规范小写代码，常见名称兼容归一（如 `english→en`），支持 `zh-tw` 等受限 BCP47 代码。上线前仍需保持 live gate=0；Graph v22.0 已有视频状态只读验证已完成，真实发帖仍需另行审批。

## V2 指标、视频制作与容量门禁

- FB 使用与运行库物理分离的 SQLite `metric_generation / metric_daily / metric_active_pointer`。MySQL 每次只按 product+platform+一个完整北京自然日流式聚合；完整写入、checksum、READY 后才原子切换 pointer，且旧 `refreshed_at` 重试不能把 active pointer 从新代回退。候选选择在一个 SQLite 读事务中冻结窗口 generation IDs，按 spend/revenue 的 ratio-of-sums 计算；缺任一日 READY 均 fail closed。小时任务仅刷新昨天（`:37`），低峰 repair 刷 1–30 日，两者共用 `flock`。单日流式 SQL 在 `ONLY_FULL_GROUP_BY` 下合法，并以正整数 material ID 的字符长度再按原字符串排序；这与任意精度数值顺序一致，避免 `CAST ... UNSIGNED` 溢出及未分组表达式错误。
- `video_template` 在新建和更新时严格必填，仅接受 `random_overlay`；缺失或其他枚举返回 `409 fb_auto_video_template_required`，没有 FB 历史兼容回退。
- 每个 Page 任务使用 `template/version/slot/page_id` 派生的稳定 GPU job ID；重试复用，同一 run 的不同 Page 不共用成片。GPU 必须返回匹配 job/content/profile、不同于源 URL 的 HTTPS 成片、SHA-256、正 size/duration；任一校验失败不得调用 Graph。
- 独立媒体 profile 复用生产验证身份 `tt-post-random-overlay-h264-720x1280-v3`；仓库内 `features/fb_gpu/prepare_worker.py` 是不导入 TT credentials/API 的 prepare-only 提取，固定 `h264_nvenc`、独立 work root/secret、CPU `18836`/GPU `8836` 隧道，HTTP 只开放 health 与 prepare。成片按 SHA 内容寻址上传 COS，只有明确 404 才创建，HEAD 同时核对 size+sha metadata，上传使用 public-read/video/mp4/sha/profile metadata 并再次 HEAD，Graph 只获得验证域名的逐段 URL 编码 HTTPS 地址。
- GPU processor 当前以进程内锁串行执行，实际并发为 1；CPU prepare worker 的并发不能被当成 GPU 并发。`max_jobs_per_slot=20` 只是在4小时 ahead 下的保守上界，未完成真实 NVENC 单任务与P95基准前不得上调。
- 启用时计算 `publishable_pages × daily_frequency`，同时作为每日 GPU jobs 和 Graph posts 估算，并按所有启用模板保守汇总最坏同槽峰值。创建运行在素材冻结后重新读取 Page/Token、旧队列、跨模板 Page 重叠和全局容量，最终事务再校验 enabled/version fingerprint。默认上限为 500 Page、20 job/单时隙、500 job/post/日、10 个启用模板，提前制作窗口 14,400 秒；无真实基准时有效启用上限由更保守的 20 job/slot 决定。超限返回中文实数/上限，不创建任务。
- `FB_AUTO_POST_LIVE_ENABLED=0` 时 scheduler 不写 slot/watermark，run-now 不创建 run，prepare/Graph 均不调用；指标只读刷新保持独立。

## 变更记录

- 2026-08-17：完成实现候选与本地测试，无生产变更。
- 2026-08-20：确认并进入 `{{desc}}`/`{{url}}` 扩展开发；生产验收保持 `FB_AUTO_POST_LIVE_ENABLED=0`，不得创建模板、运行或真实 Graph Post。
