# 封面失败诊断与通知展示补丁

## 用户授权与结论
2026-09-10 用户确认执行：失败飞书提醒、严格封面校验、真实封面展示、核验已有视频后只恢复安全失败步骤。线上 a292b352 已增加独立通知 worker 并成功发送当前任务，故本补丁复用该 outbox/event_key，不启用第二套发送器，不重复发送。

任务 7448081bb00e2fcf6590ba6cac54cebc 的要求为“要奢华一些”，V2反馈为“整个页面再加一些别的元素进来显得丰富些，另外加一些金色的感觉”。现有适配器已将原剧图实际附件、要求、全量修改意见传给生图进程。原始图 SHA 与生成前后冻结副本一致；没有保留 image_gen 具体调用实参日志，不能声称已核对其工具级参数。

第一次 V2 输出 PNG 1672×941，宽高比例落在原容差内。caBX CRC 与内容不符，下一 chunk 长度越过文件边界，Pillow 报 broken PNG file。原代码把解码与比例混为同一个 cover_invalid，属于误导性错误提示。后续用户重试已得到有效 V2 并审核通过，本补丁不修改或重生成该图片。

账本12的视频 c68YK2Xm0Yk，原 resumable 会话返回上传完成的原响应，频道/标题/大小与本地账本一致；香港 source.mp4 的323706483字节及SHA与账本相同。首次身份读取成功后 thumbnail 设置失败；随后 owner videos.list 返回 HTTP200/items=[]。旧代码未保存第一次 thumbnail HTTP/API reason，无法追溯该次具体拒绝原因。items=[]只证明当前不可读取，不能断言被删除、版权拒绝或权限原因。保持unknown、原ID、原会话、上传尝试1次、评论0次；不清空ID或创建替代视频。

## 本次实现
- 单帧真实像素解码，PNG全chunk边界/CRC/IEND及IDAT扫描线验证，拒绝修改IHDR后伪装尺寸的文件。损坏、格式、尺寸、比例分别报错；不自动修补错误图片。
- 生图超时、进程失败、无文件、空文件、超32MiB明确分类。每次私有generation-request.json保存要求、全部反馈、版本与参考图SHA，不含环境密钥。
- cover_preview只来源于已生成成功版本。当前缺图时展示最近历史版本与标签；从未成功时显示暂无有效生成封面，禁止用素材缩略图冒充。审核只允许当前版本有效图片。
- 复用独立failure-notifications.sqlite3，详情展示失败提醒pending/sent/unknown及历史属性。发送前状态读取失败恢复pending；DTO优先当前事件，避免迟到旧事件遮挡当前提醒。保留原event_key、Feishu UUID与成功回执。
- YouTube错误保存HTTP状态与白名单reason，禁存Google自由消息/请求/凭据。空items明确说明未返回视频，保持unknown和禁止重复上传。已有失败不被改写成推测的原因。
- 保留6秒轮询与DOM节点/焦点/滚动位置，不恢复全弹窗innerHTML重建。

## 验收与发布约束
所有新增自动化使用离线fixture或浏览器mock。真实图片损坏样本仅只读校验，正常V1/V2作对照。不调用真实生图、不创建测试视频/评论、不重复飞书消息。
部署脚本scripts/deploy_youtube_failure_handling.py核对GitHub exact commit、全部live SHA、已有通知worker SHA和SQL SHA。仅8个源/静态文件及3份公网静态文件；API、旧publisher、新publisher在空闲后重启，统一writer和HK不重启。备份现有文件、SQLite和独立通知库；回滚只恢复代码，保留发布账本、通知回执及资产。
实际发布提交、备份、回读结果完成后追加。
