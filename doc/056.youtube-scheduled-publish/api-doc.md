# API

POST /api/youtube-auto-publish/tasks：原请求新增 publish_at，带时区 RFC3339，空或省略=立即；统一储存 UTC。

POST /api/youtube-auto-publish/tasks/{id}/schedule：action=reschedule|immediate|cancel，publish_at（reschedule 必填），schedule_version 整数 CAS，operation_id 独立幂等标识。返回 {task: DTO}。保留 Cookie、权限、同源检查。

DTO：publish_at 当前确认时间；schedule_version；schedule_state；schedule_control {action,state,publish_at,message}；can_schedule。平台处理期间 schedule_control.publish_at 为请求时间，原确认时间仍保留。状态 scheduled / schedule_missed / schedule_pending / cancelled；can_review 独立体现封面是否可审核。失败/租约冲突返回 409，客户端保留输入。

时间与操作可先存储于 preparation；入账时携带版本。服务在同一 SQLite 事务查询账本，覆盖 enqueue 成功而 preparation 尚未回填的崩溃窗口。视频/评论事实始终以原账本为准。
