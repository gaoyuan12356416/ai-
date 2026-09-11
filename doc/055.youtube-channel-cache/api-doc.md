# API增量

权限维持现有Cookie+youtube_auto_publish+导航权限。GET /channels?refresh=1 返回 channels、checking、error、ttl_seconds、checked_at。每项eligible、auth_status、reason、checked_at、long_uploads_status；封面拒绝时thumbnail_permission=denied/failure_at。不返回Token、scope、内部account ID。

GET /materials?search=...&refresh=1 返回items和cache:{age_seconds,ttl_seconds,stale,refreshing,error}。冷miss后台读取，不得将refreshing空列表当成没有素材。

POST /channels/verify-thumbnail：仅管理员，JSON {channel_id,failure_at,confirmation:"studio_thumbnail_verified"}。须显式确认已在对应Studio核验自定义封面功能。实时预检后只解除具体历史拒绝；幂等、无平台写入，审计在/mnt/data-disk/youtube-auto-publish/channel-checks.sqlite3。
