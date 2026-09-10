# YouTube 自动发布接口

前缀 `/api/youtube-auto-publish`，使用后台 Cookie 会话和独立模块/导航权限。POST仅同源JSON，普通请求上限32 KiB，封面base64请求上限3 MiB。响应不得包含OAuth token、client secret、resumable URI或磁盘路径。

| 方法/路径 | 内容 |
|---|---|
| GET /bootstrap | settings、source configured/message、eligible channels、channels_loaded、enabled、can_manage_settings；默认保留原频道返回 |
| GET /bootstrap?include_channels=0 | 页面初始化使用：只读本地配置/设置，channels=[]、channels_loaded=false，不查询频道或素材 |
| GET /channels | 新建发布时按需加载可用频道，返回channels；同样校验Cookie、模块和导航权限，不返回scopes或youtube_account_id |
| GET /materials?search= | 服务器素材投影，最多100条；未配置返回items=[] |
| GET /tasks?search=&status=all | 本人/本租户管理员最近200条、counts、limit、bounded |
| GET /tasks/{id} | 冻结文案/素材/频道、版本、通知状态、分阶段结果、can_review/can_retry |
| POST /tasks | operation_id、material_id、channel_id、title_template、description_template、comment_template、cover_source、requirements或cover_asset_id |
| POST /covers/upload | data为纯base64 JPG/PNG；返回asset id与鉴权url，先预览后确认 |
| GET /covers/{id} | 按租户/owner校验后返回image/jpeg；no-store/nosniff |
| POST /tasks/{id}/review | version、action=reject/approve/manual；reject需feedback，manual需cover_asset_id |
| POST /tasks/{id}/retry | 仅可重试状态，复用既有视频身份，unknown根据阶段限制 |
| GET /settings | default_description |
| POST /settings | 管理员写本租户default_description；已提交任务不变 |

400校验失败、401未登录、403权限不足、404不可见资源、409状态/版本冲突、503未启用或配置/依赖不可用。对409前端刷新最新任务，不能静默审批新版本。服务端不接受客户端expanded值、素材url、scopes或用户身份。

频道前端缓存仅用于选择界面，有效期60秒；创建任务仍由服务器重新校验当前频道资格。页面不等待导航网络请求才展示工作台，任务列表有独立加载状态。前端GET请求12秒、POST请求45秒超时，覆盖响应正文读取；POST超时视为结果未知，保留草稿与operation_id，不自动重复提交。

依赖契约：[YouTube thumbnails.set](https://developers.google.com/youtube/v3/docs/thumbnails/set)、[videos.update](https://developers.google.com/youtube/v3/docs/videos/update)、[processingDetails](https://developers.google.com/youtube/v3/docs/videos#processingDetails.processingStatus)、[飞书消息发送](https://open.feishu.cn/document/server-docs/im-v1/message/create)。
