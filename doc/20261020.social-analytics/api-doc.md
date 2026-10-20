# 社媒报表 API

GET /api/social-analytics/options 返回渠道/人员/账号/剧目/语言/链接类型及渠道质量。
GET /api/social-analytics/report 查询参数：start/end UTC YYYY-MM-DD、重复platform=youtube|x、owner/channel/drama/language/link_type、group_by至多三个逗号分隔维度、sort/direction/page/page_size/search/ranking_metric。默认前7完整UTC日及owner,platform。
GET /api/social-analytics/export.csv 与report同参数同聚合。旧/api/youtube-analytics/*强制YT并兼容未加前缀channel；其他平台拒绝。

接口只接受真实Cookie会话；独立social_analytics及导航ACL；客户端不能注入_system_report。普通用户本人、管理员本租户。quality.platforms、complete、reconciliation显式表示渠道缺失及可见归因范围，不将失败伪装成0。

POST /internal/x-posts/attribution/resolve：仅127.0.0.1/::1和专用bearer；JSON仅queue_id/log_id。返回冻结上下文；映射失败503、冲突409、缺记录404。HTTP对外不暴露内部人员查询能力。
