# 049.drama-synthesis-upgrade 需求与技术设计

## 背景与目标

在不改变线上「短剧素材合成」视觉体系、侧栏、表单、卡片和任务表的前提下，扩展随机模板视频、不可变短链与异步 YouTube 发布。CPU 服继续编排；视频渲染迁移到香港 GPU `43.154.250.89`。本需求不授权部署、推送或真实 YouTube 发布/评论。

## 范围

包含：

- 四个输出项默认均不勾选；前后端都拒绝零输出。
- 新增随机模板视频：自动随机或手动选择 `border`、`opacity_video`、`corners`、`tint` 四层。
- 创建任务时解析并冻结配方版本、素材清单、素材 SHA、参数和配方 SHA；重试只复用冻结配方。
- 复用已验证的 FB v1 素材目录和 FFmpeg 图，采用独立 profile `drama-random-overlay-h264-720x1280-v1`；不包含 `light`。
- 新请求不再发送封面模板和命名规则；服务端历史默认值和历史任务兼容保持不变。
- 完成任务可复制视频 URL、创建幂等短链、创建异步 YouTube 发布任务。
- YouTube 视频与评论使用独立状态；评论只在视频确认成功后执行。

不包含：

- 不改 W2A 归因、深链、Pixel、CTA 或现有 `/tt` 合同。
- 不开放自定义短链目标，不实现通用 URL 跳转器。
- 不把 OAuth 凭证、刷新令牌、断点续传 URI 返回浏览器或写日志。
- 不迁移历史媒体、不切换生产隧道、不发布代码、不创建真实视频/评论。

## 业务规则

1. 随机模板目录固定为清单 SHA `028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f`；旧 GPU 已核对 20/20 文件、520,297,533 字节，分类 `border=3,corners=3,light=2,opacity_video=5,tint=7`；本 profile 仅使用四类并排除 `light`。
2. 自动模式以任务 ID、剧 ID、profile、版本和清单 SHA 确定性选层；手动模式必须选择全部四层。任何目录、素材或 hash 不一致都拒绝渲染。
3. 随机成片必须是 H.264 High、720×1280；结果回传 output SHA、profile、recipe SHA，CPU 验证配方身份后才完成任务。
4. 短链格式固定 `https://page.dramabuzzs.com/s2l/<id>.html`；目标固定 `https://www.dramawavew2a.com/ads/101/2284/view?cid=<job_id>&af_channel=ai_youtube`，参数顺序和编码固定。相同任务幂等复用；ID 对应目标不可变。
5. `page.dramabuzzs.com` 当前为 CloudFront/S3，CPU/HK 均无已确认发布凭证。发布适配器未配置时必须失败关闭，不得声称短链成功。
6. YouTube 频道只允许当前任务 `app_id` 下 `channel_status=1`、有 refresh token、client config、上传 scope 和身份读取 scope 的映射。仅有 `youtube.upload`、`channel_status=2` 或缺失 scope 元数据均不可用。评论还必须有精确 `youtube.force-ssl`。
7. 当前只读基线：app 1479 有 59 个映射；27 个可刷新且具备上传 scope，其中 4 个状态为 2；12 个同时具备评论 scope。所有现存 access token 均标记过期，因此只能服务端刷新。
8. 请求用 `operation_id` 幂等；同任务同频道已有成功视频时必须二次确认；已有未知结果时禁止替代发布。
9. 断点续传 session URI 必须在首个数据 PUT 前持久化。中断/5xx 后先用空 PUT 和 `Content-Range: bytes */<len>` 查询；308 续传，完成响应复用视频 ID，404 视为未知并失败关闭。

## 技术设计

- 现有 `drama_material_job` 保持主状态机；SQLite 增量增加配方、短链、YouTube 任务和事件四张表，由 `ensure_storage()` 可重复创建。
- CPU：`app.py` 负责校验、冻结、API、审计和向 `127.0.0.1:18788` 提交 HK GPU。
- HK GPU：独立 8788 HTTP 监听和独立反向隧道；上线时保留旧 18787，完成 canary 后再切 CPU URL。
- YouTube：单独 systemd 异步 worker，从既有 MySQL 表按需读服务端凭证；SQLite 只存任务状态和发布身份。
- 每次刷新 access token 后，以及创建 resumable session/发布评论前，必须用 `channels.list(part=id,mine=true)` 核验唯一频道与冻结 `channel_id` 完全一致；空、多频道、不匹配或未授权均失败关闭，网络/5xx 仅可在尚无外部写入时安全重试。
- YouTube task claim 使用持久化 `lease_generation` fencing；claim 原子递增 generation，所有后续状态写入必须同时匹配 task、owner、generation。下载过程续租，并在每次外部调用前续租，过期 worker 不得覆盖重领 worker。
- 短链：当前实现不可变文件系统适配器，只有 CloudFront/S3 所有者提供受审计发布挂载后才能配置。

## 验收标准

- UI 四项默认未选；零输出前后端均拒绝。
- 自动/手动配方可冻结，任务重试不变；目录或回传身份不一致失败。
- 随机产物在列表/详情展示；原三类产物继续工作。
- 新 payload 不含 `cover_template`、`naming_rule`；历史数据仍可读取。
- 短链目标、HTML、编码、幂等性、不可变性和无开放跳转均通过离线测试。
- YouTube eligibility、token 频道身份核验、lease generation fencing、视频/评论分态、断点重试、未知关闭、幂等、二次确认均通过 fake-client 测试；测试不访问真实 Google API。
- 语法、定向测试、diff check 和秘密扫描通过；独立 QA 后才允许部署。

## 风险与待决依赖

- P1：CloudFront/S3 短链发布所有者和审计发布路径尚未确认；保持失败关闭。
- P1：公网已存在 `/s2l/1.html`，启用 publisher 前必须由 CDN owner 冻结/核对既有 ID 命名空间；本实现遇到对象内容冲突会拒绝覆盖。
- P1：HK 尚未安装 drama renderer/素材目录；必须按 deploy.md 验证清单后 canary。
- P1：生产源视频 HTTPS hostname allowlist 需部署前从实际 COS URL 确认，不得使用通配符。
- 当前实现候选只进入独立 QA，不部署、不推送。

## 变更记录

- 2026-08-26：冻结需求合同、只读生产基线和实现候选范围。
