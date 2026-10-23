# X 个人视频发布

## 交付范围

在现有 X 账号列表与 X Post 执行链上增加个人手动、Excel 和个人账号素材池发布。账号列表保留现有统计、剧语言、允许发布、同步/校验交互；按已确认截图移除账号资料、授权权限、授权人、授权时间、Access Token/自动续期、最近校验/资料同步、更新/停用时间七列，剩余八列。

`/x-video-publish.html` 默认手动发布，`?view=records` 查看记录。管理员的账号管理仍进入 `/x-account-list.html`；个人操作只对当前登录人所有的账号可用。普通用户在工作区查看自己的账号。

- 五段纵向操作：选视频 → 选候选账号 → 逐素材手工配对 → 每配对独立文案/预览 → 原片或随机加工。
- 选择候选账号不会创建配对；同素材多账号使用同一北京时间，空时间立即排队。不同 Excel 行可分别预约到秒。
- `{name}`、`{desc}`、`{url}` 可选，支持纯手写文案；展开后超过 280 加权字符明确拒绝，不静默截断。确认页短链为 `preview.invalid` 占位，发布时使用对应日志的真实短链。
- XLSX 由随包 SheetJS CE 0.20.3 本地解析，最多 2 MiB/200 非空数据行、五列。文件不上传；服务端只接收规范化行并再次验证 owner、账号状态、来源、语言、文案、时间及历史重复。
- 手动批次任何无效配对都会阻止整批提交；Excel 可确认有效子集。历史成功配对需明确确认再次发布；进行中/待核对配对始终禁止重复。
- 预检有效期 10 分钟。确认后冻结配对和文案；修改默认文案不改已有任务。宏源信息在执行前漂移会明确阻止，不悄悄改变已确认文案。
- 草稿按登录人保存在服务端，不读取或导入原型的虚构 localStorage。
- 素材池首次保存暂停、入池保持输入顺序、允许排序和批量移除。每实际北京时间时点取队首一条；空池记录跳过。未知结果暂停；暂停不撤销已生成任务。调度启动前和超过一分钟的时点不补发。
- 在原账号列表改变剧语言时，同一事务暂停对应个人素材池；不匹配素材保留并提示，已有任务不改写。

## 权限与执行边界

新增模块权限 `x_video_publish`，普通用户默认关闭。页面导航 `xVideoPublish`、`xVideoPublishRecords` 按模块与导航配置共同控制，保留原 X 分组的 `x_accounts` 要求，使用者需同时具备这两个模块权限。原分组和自定义子页面的权限继承不变。管理员查看全部账号不会获得代替其他 owner 发布的权利。

浏览器 `/api/x-video-publish/*` 只接受真实登录 Cookie；拒绝 API token、跨站 JSON、重复 Content-Length、Transfer-Encoding。主体来自登录用户，并从当前用户表重新校验 active/权限，不相信浏览器的 actor/tenant/user_id。

主 API 与 X sidecar 复用现有 backend loopback bearer。个人 API 与 pool start/tick 的 backend/daily bearer 分权，不给浏览器调度入口。身份与素材 broker 只监听主 API 内部路由，拒绝转发头，素材只允许固定查询操作，不接受 SQL。

素材查询使用主 API 现有只读来源配置 `ADMIN_MAPPING_MYSQL_*`，固定 `101.32.56.53:63350`，经主机 `/usr/bin/mysql` FIFO 包装器；本模块并发上限 2。每次查询不超过三个有界 SELECT，映射过大整体拒绝。预览不探测或下载媒体；执行时仍沿用现有媒体、语言、授权与容量校验。

批次、配对、幂等键、素材占用和旧手动子任务在同一 SQLite 事务冻结。每配对对应一个旧执行器子任务；同一素材共享一个预约，仅允许同一不可变批次内的已冻结 sibling 使用。未知结果阻止后续配对。每次 X 请求前复核 owner 和实时后台权限；旧任务不依赖新的个人身份 broker。

使用原 `x-post-manual` 执行器与原 token/发布日志/重试保护，不新增独立 X 发布器。新任务的文案协议为 2；旧任务保留协议 1 和分钟预约约束。

## 部署开关与上线顺序

此提交交付代码与离线验证，不代表已部署或已成功向 X 发布。禁止用真实账号/素材自动做发布验收。

1. 将审核通过的提交推送 GitHub，服务器只部署该精确提交。确认 `/mnt/data-disk` 挂载 UUID 为 `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`。
2. 记录当前 X release、API 源文件与服务状态；在线 SQLite backup 同时保存 accounts/Post DB，记录旧表行数、账本关键列摘要及 token 文件摘要（摘要不含 token 内容）。
3. 先在备份副本验证迁移。X release 应包含完整同版本 `features/x_video_publish`、X Post/OAuth 代码和两个手动/日常 runner；不要混用新版子任务与旧 runner。
4. 主 API 当前目录是组合源码，不能整份覆盖 `app.py` 或共享导航。只合入本提交的模块权限、导航增强、GET/POST broker/API 路由和静态文件路径改动，保留期间已上线的 FB/其他功能。检查实际 `drama-material-api.service` 环境和导入路径。
5. 发布新增静态资源（包括 vendor、XLSX、许可证）及八列表格；合并 `quick-nav.js`/`navigation.json` 增量。保留原导航禁用设置与访问范围。
6. 默认 `X_VIDEO_PUBLISH_ENABLED=0`。先完成主 API、sidecar 的 Cookie/403/owner/只读源/页面联调，验证旧 X/FB 路径和旧账本不变。
7. 正式开放时在 `/etc/x-post-automation.env` 设置 `X_VIDEO_PUBLISH_ENABLED=1`，定向重启 `x-post-automation.service`；现有已确认任务仍由原 manual timer 执行。给明确的用户分配新模块权限。
8. 如需素材池调度，安装 `deploy/x-video-pool-scheduler.service`，在 `/etc/x-video-publish.env` 设 `X_VIDEO_PUBLISH_POOL_SCHEDULER_ENABLED=1` 后启用该 unit。它只复用 `/etc/x-post-daily.env` 中 daily bearer，不持有 X token。未显式启用的个人池继续暂停。

主 API 新内部路径须保持公网不可用：`/internal/x-video-publish/identity`、`/internal/x-video-publish/source`。sidecar 新路径为 `/internal/video-publish/dispatch` 与 `/internal/video-publish/pool/{start,tick}`。

## 回退与保留账本

先关闭新建开关、停止新的 `x-video-pool-scheduler.service`，回退静态入口；暂停不撤销已经冻结的任务。保留新版 executor/core 直到个人 v2 待执行、处理中和未知结果全部核对，避免旧代码错误解读个人多对多预约。

**有真实任务或平台副作用后不得直接恢复旧 SQLite 备份**，否则会抹掉幂等键、平台结果和占用记录。完整代码回退须先停相关领取器、核对所有 v2 非终态/未知任务，再按已记录的旧 release 切回；追加表可保留。旧平台日志、旧任务和 token 文件无需重写。

## 验证

全部自动测试使用临时 DB、虚构账号、fake HTTP/来源；不连接生产发布接口。

```powershell
python -m unittest scripts.test_x_video_publish_core scripts.test_x_video_publish_pool scripts.test_x_video_publish_gateway scripts.test_x_video_publish_runtime scripts.test_x_video_publish_language -q
python -m unittest scripts.test_x_post_priority_manual_store scripts.test_x_post_manual_runner scripts.test_x_post_manual_sidecar scripts.test_x_post_auto_template_bridge scripts.test_x_post_ledger scripts.test_x_account_language_routing -q
node tests/x_video_publish_frontend_test.cjs
python -m compileall -q features/x_video_publish features/x_accounts/oauth_service.py features/x_posts/service.py scripts/x_video_pool_scheduler.py app.py
git diff --check
```

浏览器验收使用实际静态页面、实际 `PublishingService`/`PersonalStore`、临时 SQLite 与虚构来源，所有外部请求禁用。覆盖 A→2/B→1、重复请求一个批次、默认文案、入池失败行保留、排序和启停，以及 1440/820/390 响应式布局。真实 X 响应、生产源配置与生产部署需要上线时单独核对，不能用离线通过替代。
