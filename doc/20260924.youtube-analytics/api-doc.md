# API

GET /api/youtube-analytics/options：当前身份可访问冻结记录的owner/drama/channel/link_type/language选项。

GET /api/youtube-analytics/report：start/end(YYYY-MM-DD, UTC)、group_by(逗号分隔1–3个date/owner/drama/channel/link_type/language)、owner/drama/channel/link_type/language(可重复，单项最多100)、search(剧名/ID)、sort/direction、page/page_size(20/50/100)、ranking_metric。

返回 totals、rows、trend、ranking、pagination、group_by、quality(missing_dates,unmatched_campaigns,unmatched_totals)、meta(日期/UTC/USD/快照时间/权限范围)、warnings。金额USD，率百分数；缺失/null不得显示0。趋势和排行榜采用全部筛选结果，不局限当前页。

GET /api/youtube-analytics/export.csv：相同参数，导出全部分组；UTF8 BOM/公式注入防护，含统计范围与数据缺失口径。

全部请求先检查Cookie和youtubeAnalytics导航及youtube_auto_publish模块。GET-only，无平台或本地业务写入。错误400参数、401未登录、403无权限、503数据暂不可用；均no-store。
