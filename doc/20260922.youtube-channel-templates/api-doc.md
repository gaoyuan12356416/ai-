# 接口

统一前缀 `/api/youtube-auto-publish`；需真实 Cookie 和 youtube_auto_publish 权限。JSON no-store；保存同源 JSON，上限32KiB。

- GET `/channel-list?refresh=1`：`channels` 为现有安全频道 DTO，附 `channel_url` 和 `template` 摘要。摘要含 `configured_fields` (title/description/comment)、version、updated_by、updated_by_name、updated_at。保留 checking/error/checked_at 等异步核验状态。受 youtubeChannelList 导航鉴权。
- GET `/channels/{local_id}/template`：返回 `{template:{channel_id,title_template,description_template,comment_template,version,updated_by,updated_by_name,updated_at}}`。不存在模板返回空文本、version=0。youtubeAutoPublish 或 youtubeChannelList 任一允许即可读取。
- POST `/channels/{local_id}/template`：请求包含真实 channel_id、非负整数 version、三个完整文本字段；响应同读取。受 youtubeChannelList 导航鉴权；actor 来自会话。三项为空也保留行并递增版本，避免清空后出现版本重置问题。

字段校验复用现有 render：空模板跳过必填校验；非空模板检查类型、长度、宏语法。实际素材展开及最终长度仍在创建任务时校验。无自动宏展开或测试短链分配。

错误：401 未登录；403 模块/导航/同源限制；404 频道不存在；409 template_conflict 或 channel_identity_changed；400 非法字段/宏/版本；503 数据源暂不可用。冲突和失败保留输入，主动“载入最新模板”会替换编辑器输入。

发布 POST `/tasks` 请求与幂等摘要契约不变：继续提交最终 title_template/description_template/comment_template。
