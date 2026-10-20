# Facebook视频发布：来源、宏与短链合同

本文件描述独立 `features/fb_video_publish/{sources,templates,links}.py`；不修改现有YouTube、FB自动模板及其归因规则。生产入口需先完成Cookie、模块权限和可信actor构造；本适配器不接收前端提供的身份或用户映射。

## Page与素材读取

`SourceRepository(page_repository, material_repository)` 复用现有FB的Page池授权及语言别名。新功能使用独立 `BrokerReadOnlyMySQL(env)`，经带内部Bearer的固定本机接口转交主API的 `GatedReadOnlyMySQL(env)`；不改变旧自动发布仓库。主API以 `/usr/bin/mysql` 进入现有FIFO Gate，固定副本 `101.32.56.53:63350`、`@@read_only=1`、`START TRANSACTION READ ONLY`、连接3秒、SQL8秒、进程25秒、并发2。sidecar保持原专用用户，不增加FIFO权限，不使用人工保留的直连连接池。

本机协议为 `POST http://127.0.0.1:8787/internal/fb-video-publish/query`，JSON包含 `sql,params,schema,blacklist_schema`，成功返回 `{ok:true,items:[...]}`。公网nginx拒绝该路由，主API另验loopback、内部Bearer与固定表白名单。请求最多256KiB，响应最多16MiB；不跟随重定向、不读取代理环境。数据库密码只进入子进程环境，字符串参数使用UTF-8十六进制字面量`_utf8mb4 0x…`绑定，既无SQL引号注入，也保持COERCIBLE排序规则兼容真实unicode_ci列；不使用会产生IMPLICIT排序规则冲突的`CONVERT(hex)`。XML结果只在内存解析，错误不包含SQL/token/stderr。

broker使用受限仓库SQL词法，不接受通用SQL：逐字符识别、每层FROM范围、精确schema.table及函数白名单。除8秒执行hint外不允许注释；逗号表列表、TABLE/CTE子查询、未知/带引号/带schema函数均拒绝。全部15种实际Page与素材查询模板经独立集成测试验证兼容。

- `list_pages(actor, q='') -> list[PageDTO]`：actor包含 `is_admin: bool, business_owner_id`。普通用户按Page池负责人过滤，管理员按现有管理员范围读取；只接受app_id=1479、产品Dramawave的有效Page池。按真实PageID去重；返回名称、语言、group_ids、eligible_token_count、legacy_conflict及blocked_reason，绝不返回token。旧版启用队列与真实Page重叠时标记禁用，失效授权Page保留展示。
- `get_pages(actor, page_ids) -> dict[PageID, PageDTO]`：每次重新读取；1–200个不重复ID；不可见Page返回404；异常授权或旧版队列占用返回409。配置可共享，但每次使用仍按调用者当前可访问范围过滤。新旧FB自动任务的活动/unknown锁由核心事务处理。
- `search_materials(query) -> {items,next_cursor,has_more,limit}`：query包含search、language、uploader_id、cursor、limit；默认50、最大100，语言/搜索/上传人均在SQL LIMIT前过滤。搜索匹配源素材ID、素材名，或同content_id/规范语言/app1479已上架的剧名。数值ID倒序，cursor冻结首次上边界、末次扫描ID和筛选摘要；改变筛选必须重开第一页。不返回昂贵全量计数。
- `get_materials(material_ids, need_desc=False) -> dict[MaterialID, MaterialDTO]`：1–200个不同源素材ID，最多每100个一批；每批一次素材查询、一次剧名查询、一次简介查询。无逐素材网络探测，无逐Page重复SQL。缺失、重复、越权来源、歧义元数据统一失败，不返回可被误当作完整成功的子集。

素材是 `ads_custom_source` 中data_source=6、product=Dramawave、type=2、is_delete=0的源视频。须有content_id、有效剧语言、0 < duration <= 3600秒；精确关联app_id=1479、release_status=1且0 < deploy_time <= 当前时间的同语言 `ads_drama_info`。排除Facebook发布黑名单的直接content_id及series_code。缺时长不在请求中ffprobe，不猜测。

媒体只接受既有广告素材COS主机 `advertising-1306474899.cos.ap-hongkong.myqcloud.com` 或已批准CDN `socialkit-cdn.yingliang.tech` 的MP4；旧HTTP地址仅在COS同主机规范为HTTPS，CDN必须原生HTTPS，拒绝其他主机/凭证/端口/fragment/控制字符。缩略图采用相同主机HTTPS规则，非法缩略图清空。详情预检还须由发布执行器完成实际媒体校验。

MaterialDTO字段：material_id、material_name、content_id、language、drama_name、drama_description、material_tag、media_url、duration_seconds、thumbnail_url、uploader_id、description_status。剧名取同语言已发布剧资料，简介取app_id=1479/type=2的同语言 `ads_drama_resource`，重复相同值合并，冲突不猜。批量资料查询限制1000行并在SQL按本批语言缩小范围。material_tag取源表tag_name，空时归因用none。

## 文案接口

`validate_template(text) -> str` 去首尾空白，要求非空、UTF-8合法、模板不超过16000字节；仅 `{name}`、`{desc}`、`{url}`；孤立/嵌套/未知花括号均拒绝。控制字符仅允许换行、回车、制表符。

`render_template(template, material, short_url='') -> str` 单次替换，剧名和简介对应DTO字段；来源中的宏外观文本不再展开。实际出现的宏缺来源返回409。`{url}`必须为 `https://gy.g2flow.com/s2l/facebook/<正整数>.html`。最终文案以真实已分配短链计算5000 UTF-8字节上限；此校验必须在核心分配真实发布记录ID的同一事务中执行，失败整体回滚。

`render_preview(template, material, placeholder='[提交后生成独立短链]') -> str` 是只读预览接口。先按最大19位正整数记录ID的真实短链校验5000字节预算，再展示不可访问的占位文本，不分配记录或生成文件。预检批量取素材时 `need_desc=False`；仅具体包含 `{desc}` 的素材×Page配对要求有效简介，避免一个配对要求简介导致其他不使用简介的配对被误拒。

## 八字段归因与不可覆盖文件

`build_attribution_url(context, ledger_id, created_at_utc) -> str` 的context包含：
`page_id,page_name,material_id,material_name,content_id,language,drama_name,tag/material_tag,sub_user_id,task_id`。

固定目标 `https://www.dramawavew2a.com/ads/101/2310/view`，固定顺序八参数：

| 参数 | 值 |
|---|---|
| c | `yingliang_post_CLV_VL_facebook_{PageID}*{真实发布记录创建秒}none{素材语言}*{剧名}*{标签或none}*{真实发布记录ID}` |
| af_adset / af_adset_id | Page名称 / 真实PageID |
| af_ad / af_ad_id | `素材名_contentid[content_id]` / 源素材ID |
| af_channel | 主API从可信创建者租户账号email映射所得唯一有效sub_user_id |
| af_c_id / af_dp | 该素材×Page的32位hex任务ID / content_id |

使用UTF-8 URL编码（空格为`%20`），保留`*`分隔；拒绝campaign片段含`*`、控制字符、无时区时间、无效ID。映射邮箱和唯一有效sub_user_id必须由主API可信解析，不接受浏览器直接指定。

`short_url(ledger_id)` 生成独立Facebook地址；`publish_link(root,ledger_id,long_url,owner_user='') -> {short_url,long_url,sha256,reused}` 只允许工作进程在冻结账本后调用。新FB命名空间和目录不复用YouTube的数字ID文件空间。

文件发布采用已验证的YouTube不可覆盖算法的独立窄实现（当前FB基线不含YouTube模块）：临时文件fsync、hardlink原子创建、已有文件仅接受相同字节、目录fsync及落盘SHA读回；拒绝符号链接。生产必须传Linux专用owner并预建0750目录，文件0640。HTML保留最大512字符fbclid。主worker还需在确认队列可领取前读取公网地址核对相同SHA。

重试复用原任务/记录/长链/文件，禁止重新分配短链或覆盖文件。`publish_link`本身不创建账本、不更新状态、不发送视频；公网未验证之前不得发布视频。

## 离线验证

`python -m unittest tests.test_fb_video_source_links -v` 使用内存模拟只读仓库和临时目录；覆盖严格宏、缺宏源、真实短链字节边界、八字段UTF-8、独立配对、冻结文件冲突与并发复用、Page负责人过滤、异常授权与旧队列、keyset跨筛选/失效、200素材分批、媒体拒绝、元数据歧义。无线上SQL、token、消息、短链或视频写入。

2026-10-20 当前来源/链接套件29项通过；事务/worker套件34项、gateway套件9项也通过。SQL适配器测试覆盖固定副本、FIFO入口、只读验证、参数绑定、XML字符/NULL、超时终止、内部协议及错误脱敏；用真实Page池、成员、旧队列、凭证和素材仓库生成的7种SQL验证broker白名单兼容。

## 真实只读schema验证

2026-10-20，经CPU服务器的技能SSH入口，在进程内加载待审模块，没有落服务器代码文件；查询使用副本63350、只读事务、3秒连接/8秒SQL/12秒读取上限，每次回滚关闭，只有一个并发连接。未调用Graph、GPU、短链发布函数或创建任务。

- `search_materials(limit=2)` 及随后两个源ID的 `get_materials` 共6条SQL返回3/2/1/2/2/1行，用时1.914/1.866/2.246/1.890/2.042/1.879秒。两条DTO语言en，时长1038.0/962.0秒，同剧语言关联成功、简介matched、媒体COS HTTPS，has_more=true。确认返回material_id、material_name、content_id、language、drama_name、drama_description、material_tag、media_url、duration_seconds、thumbnail_url、uploader_id、description_status。
- 新增剧名筛选后，取一个已知同语言剧名并执行 `search_materials(search=该剧名,language=en,limit=1)`；返回1条且剧名完全一致。四条有界SQL用时1.824/1.860/1.944/2.010秒，证明同语言EXISTS查询及真实字段可运行。
- `GatedReadOnlyMySQL` 通过服务器root进程使用真正的 `/usr/bin/mysql` FIFO wrapper进行单行源ID查询；`@@read_only`校验成功，返回仅material_id列、值与请求相同，耗时2.100秒。sidecar→主API broker的完整路由尚需部署后回读；此条不冒充生产发布验收。
- 继续用真正FIFO执行完整素材SQL发现1267排序规则冲突，修正参数绑定后再次验证：主键窗口7677074–7697073、时长10–30秒、LIMIT2；源查询2.347秒，再同语言剧名/简介补全，全程8.868秒。得到material_id=7690181、content_id=QNtQIzkxUs、ar、10.0秒、COS HTTPS可用候选，URL及DTO保存本地`output/fb-video-short-material-candidate.json`供隔离prepare-only验证。此次仅查询，没有调用GPU、Graph或写生产文件。
