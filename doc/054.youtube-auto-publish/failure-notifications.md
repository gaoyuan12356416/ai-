# YouTube 发布流程失败飞书提醒（2026-09-10）

用户明确要求发布过程失败发飞书提醒。实现使用独立failure_notifications.py和原worker的小范围调用，不修改service/runtime/engine、页面、审核及上传状态机，避免与同模块其他开发互相覆盖。

## 本次故障证据

任务7448081bb00e2fcf6590ba6cac54cebc，发布账本12，YouTube视频ID c68YK2Xm0Yk。18:51:37已完成323706483字节上传；18:51:45出现真实thumbnail running→failed，错误码youtube_thumbnail_set_failed。18:52:58后一次重试在状态核对时变为unknown，错误码youtube_video_reconcile_unknown；thumbnail unknown、processing/public pending、首评queued。

只读Google videos.list(part=snippet,status,processingDetails,id=该视频)返回HTTP200、items=[]。这是当前视频不可核对的直接证据，不能据此声称封面比例不合格或频道权限被拒绝。第一次thumbnails.set的HTTP状态码和API reason没有被旧实现持久保存，无法还原其确切拒绝原因；不得把第二次状态查询错误当成第一次封面接口错误。

## 通知规则

- 覆盖封面生成、发布准备、上传、设置封面、处理、公开及首评的已落库失败/未知终态；正常processing和自动重试中的暂态不刷屏。
- 只读主SQLite准备/发布表，只观察reviewed_thumbnail对应准备任务。独立outbox位于/mnt/data-disk/youtube-auto-publish/failure-notifications.sqlite3，权限0600，不修改任务或触发重试。
- 收件人为原任务创建者，优先open_id；沿用AI后台同一飞书应用。红色卡片包含标题、频道、失败阶段、原因、错误码、任务/视频ID和登录详情链接，没有自动操作按钮。
- task/version/phase/code/失败更新时间组成稳定event_key，同一失败只发送一次；人工重试后新失败可再次提醒。发送前再核对当前失败仍存在，已恢复/重试旧事件superseded。
- 先持久化sending/lease，再发送稳定Feishu UUID并保存message_id。超时、缺回执或进程中断保留unknown，不盲目重发。发送异常不阻断发布流程。
- worker在上传迭代结束后、进入可能较慢的生图前先检查通知，生图/准备迭代结束后再检查；后台空闲时也检测已有未提醒失败。本次现有失败会补发。

## 验证和部署

11项专项测试通过：全部阶段、当前失败去重、首评、准备失败、历史legacy隔离、源库字节不变、人工重试新事件、发送未知不重放、崩溃租约、已恢复事件和真实发送合同/回执。原发布引擎43项通过，Python语法、diff及live feature guard通过。

分支codex/youtube-publish-failure-alerts-20260910，基于224ea51。GitHub准确提交归档后使用scripts/deploy_youtube_failure_alerts.py，只安装新模块和worker脚本。部署前要求无活动生成/上传；使用SIGTERM请求worker在当前迭代结束后退出，由已有Restart=always重新加载，不启用systemd stop超时杀任务。不重启API/旧发布worker/统一writer，不改变SQL和主SQLite。

2026-09-10 19:10上线，准确运行提交`a292b352cd0633f8b8b96eb853a6ea735f1671d4`。CPU再次通过11项专项和43项引擎测试、准确基线及空闲检查。备份`/mnt/data-disk/deploy/youtube-auto-publish/backups/failure-alerts-20260910-191028-a292b352cd06`；worker平滑退出后PID3078069→3112993，其余API/旧发布worker/统一writer的PID及启动时间不变。service/runtime/engine和SQL摘要不变，账本12的status、video_id、video_attempt_count、error_code、updated_at_utc完全不变。

当前真实失败通知已发送：outbox仅1条sent，message_id=`om_x100b651d4dca04a0de7047f91de0bd7`，event_key=`feefc4292a82c7b8d0c76599e051b216a5c31ac7e2be56865318b241609491e4`。这是飞书接口成功回执，不代表收件人已读。未创建测试消息，未重新上传、设置封面、公开或评论。

准确回滚命令（保留失败通知outbox以防重新启用时重复提醒）：

```sh
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/a292b352cd0633f8b8b96eb853a6ea735f1671d4/scripts/deploy_youtube_failure_alerts.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/failure-alerts-20260910-191028-a292b352cd06
```

上线前后证据保存在该release的`.failure-alert-before.json`及`.failure-alert-verified.json`。后续若主线程改动worker入口，须保留failure notification调用；不要覆盖整个旧版本worker。发送unknown需先核查，禁止清空outbox盲目补发。
