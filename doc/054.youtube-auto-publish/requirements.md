# YouTube 自动发布

## 已确认范围
2026-09-10 用户确认原型并授权生产开发。页面沿用 AI 后台的 QuickNav、UiTopbar、浅色背景与蓝色按钮。生产不出现演示数据、模拟飞书按钮、重置演示或模拟异常入口。

选择一条视频素材、一个 DramaWave 频道，填写标题、描述和可选首评。默认描述为 `Watch more exciting short dramas on DramaWave. Subscribe for more stories!`，管理员按租户设置，单次发布可修改。标题、描述、首评均支持单层宏 `{url}`、`{desc}`、`{name}`，分别为剧集跳转短链接、剧情简介、剧名。替换只执行一次，禁止未知宏，提交后冻结模板和展开值。

## 两种封面流程
本地 JPG/PNG（不超过 2 MB，至少 320×180）先解码、去元数据、保存不可变预览，再确认提交。AI 必须以素材所属剧集的原剧封面为参考图，结合剧名、简介及必填封面要求（最多2000字）生成真实16:9图片。原剧封面取ads_drama_resource.cover，按精确content_id和素材语言匹配；不能用素材缩略图替代。缺失、多个不同封面或非法地址时阻止AI提交，允许选择本地上传。首次worker生成前下载并保存私有参考图，首次及打回重做复用同一原图及修改意见。审核/详情展示保存的参考原图，供与生成结果对照。

生成成功后发送飞书卡片给任务创建者；卡片仅打开后台审核页，不能绕过登录或直接审批。审核支持打回重做（意见必填）、通过、上传替代图片预览后确认。记录每版图片、意见、审核者和时间，旧版链接不能批准新版本。

审核确认后：上传为私享 → 设置已审核封面 → 等待视频处理完成 → 将同一视频公开并读回确认 → 首评。未填首评显示已跳过；首评失败不撤销已公开视频。结果未知时禁止盲目重发视频/评论。

## 素材 SQL 预留
服务器 `YOUTUBE_AUTO_MATERIAL_SQL_FILE` 指向可替换的独立文件。当前已按用户2026-09-10确认的五项WHERE条件启用，不回退其他素材池，不接受浏览器传入SQL。原剧信息和原封面查询与素材筛选保持独立。

最终 SELECT 需要投影 `id,name,url,thumbnail_url,content_id,source_job_id,source_kind,macro_name,macro_desc,language,duration,size,app_id`。固定产品 app_id=1479；id 为唯一正整数。查询限定可信 HTTPS 媒体域、最多 100 条、缓存 20 秒、只读副本、8 秒服务器超时/25 秒进程超时、最多两个并发查询。搜索条件以十六进制文本加入外层查询，再限制最多返回 100 条，因此可搜索整个筛选范围。

当前素材筛选以 `deploy/youtube-auto-material-source.sql` 的五项条件为准。素材范围与剧集信息读取分离：按 `data_source_id → content_id` 和素材语言，从 `kunlunads_dev.ads_drama_resource` 读取真实剧名、简介。重复集数记录的相同资料去重；资料冲突或语言不匹配时明确提示，不用视频文件名替代剧名。

`{url}` 复用 ensure_short_link 和原长链规则：`https://www.dramawavew2a.com/ads/101/2284/view?af_dp={content_id}&c=ai_youtube&af_channel=ai_youtube&af_c_id={link_job_id}`，参数 UTF-8 percent-encode。短链仍为 `https://gy.g2flow.com/s2l/youtube/{id}.html`。已有源合成任务的素材继续使用原 `source_job_id/source_kind`；无源合成任务的独立素材，经剧集关联验证后使用本次发布准备任务 ID 作为 `link_job_id`，短链类型为 `custom_source`。不回填或伪造 `source_job_id`。本次发布 ID 按租户、创建者和提交标识确定，重复提交、短链生成后响应失败再重试，均复用同一链接身份；发布账本使用相同归因 ID。

## 权限与数据
独立模块及导航权限；仅真实 Cookie 会话，拒绝 API Token。普通用户仅访问本租户本人任务，管理员仅访问本租户任务。频道沿用现有 DramaWave 产品级 OAuth 频道池；不会改变频道授权归属。封面下载同样鉴权。共享 SQLite 添加独立准备表、资产表、设置表、通知表；共用原 YouTube 发布账本 ID，按 workflow 分离 claim，防止 HK 媒体缓存 ID 碰撞。视频仍使用既有 HK executor。

## 验收限制
业务状态机、权限和UI用隔离mock验证，不制造真实公开视频、评论或飞书测试消息。原剧参考生图可在独立目录以真实素材进行单图验收；不写入生产任务、资产、通知或发布账本。正常用户提交和审核的已有任务不得被部署中断。


## 2026-09-10 失败诊断与通知展示

失败处理补充：所有已落库失败阶段通知创建者，沿用独立通知outbox去重；图片损坏/格式/尺寸/比例分别提示。详情展示真实生成版本和通知回执，禁止素材缩略图冒充封面。已有video ID先核验，未知时不重传。详见failure-handling.md。
