# API

`/api/fb-video-publish` 前缀；飞书Cookie、fb_video_publish模块、fbPostMaterialPool导航权限。租户及身份来自主API。

GET /page-groups -> items 当前可访问Dramawave组。
GET /material-pool -> pool/items/counts/next_cursor/has_more/feature_enabled；state/language/limit/cursor过滤。
POST /material-pool/items/resolve -> material_ids，返回items/errors，不写入。
POST /material-pool/items -> material_ids；POST /items/remove -> item_ids，限未绑定项。
GET/POST /material-pool/schedule -> group_ids,schedule_mode,times,daily_batches,caption_template,processing_mode=original。
POST /material-pool/enable、/pause；POST /preview只读配对，可返回language_capacity。
GET /material-pool/runs；GET /operations/{operation_id} 查询当前用户操作结果。

写入携带UUID operation_id、expected_version；同请求幂等回读，操作内容不符或版本过期409。响应不明先查原操作，不能换ID重试。

私有group-pool-start/group-pool-tick只经sidecar Bearer入口；身份broker可选navigation_key只允许fbPageManagement或fbPostMaterialPool，旧协议保持默认。
