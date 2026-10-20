# 当前验证状态

2026-10-20 已正式切换主 API、YouTube 入库和日报读取代码；完整历史补传正在执行，未宣称补传完成。

入库实现提交为 e3f11ce5708df33d4c68ef2010d81aa51b03e019。备份点为 /mnt/data-disk/ai-cover-material-source/deployments/20261020T033316.881286Z，包含主源库、YT 同步库、共用上传账本在线备份及原配置。apply 成功，主 API PID784884；仅替换上传回调后的 app SHA256 为 cc68f0f4d5f7bf22ba7c6a1fbe50c56f4db360f4ea565b9fc62859055fe4405e，其余 AST 和六个独立制作 worker 保持不变。

最终离线验证共202项：共享客户端62项、历史发现与续对账12项、受控拒绝恢复7项、旧YT回归20项、日报含交互64项、部署23项、有限补传14项。Windows和Linux均验证相关核心测试。真实日报复验发现的多行 requested_country 展示误用循环变量已修正逐行取值并增加回归测试；不影响正式 country、入库结果或迁移计数。日报独立修正提交8424ded609372ca12709fca60e5b0a1bd88cf096已从GitHub部署，备份/mnt/data-disk/deploy/ai-image-daily/backups/20261020T034618.851867Z。

最终复查收紧缺回执规则：即使正式素材已读回，缺有效data.id仍为unknown，保留正式证据、禁止重发；先前confirmed且缺回执的submit也不能直接跳过。新增/调整超时真实已存、重复响应、缺id、顶层id、非法持久化id及YouTube pending测试。生产当时已确认58张全部具备有效api_id，不存在需要纠正的误确认记录；守卫更新不改变T和冻结清单。

真实样本：

- DramaWave原正式7692865 → 新接口data.id 14690193599 → 新正式7693773，产品Dramawave、剧目DXLiWAil6M、type1、人员248、1:1、US/en及签名均通过。日报真实只读合并得到正式素材2、原图版本1、迁移1、未映射0。
- Moboreels 首次业务403，用户授权同签名重试一次；data.id 14690350218 → 正式7693983。用户确认正式国家由剧库覆盖、允许空值，现已只读对账为 confirmed；累计两次尝试，没有第三次上传。
- MoboShort data.id14690875110 → 正式7695181；DramaWave 横图14690878492 → 7695183、竖图14690881718 → 7695190；YouTube14690884712 → 7695193。产品、剧目、图片类型、人员、语言和比例全部回读通过。
- FreeReels 近七天没有历史候选。窗外五月素材验证请求业务403、没有 data.id，单独记录为 validation；用户确认当前没有 FreeReels 封面上传，因此不再重试，不纳入正式补传失败统计，也不能声称979生产验收通过。

正式 T 为北京时间 2026-10-20T11:40:46.010664+08:00，窗口从 2026-10-13 同一时刻开始，右端不含 T。冻结前用 Nginx 暂停新手动任务、持有自动封面 cron 锁、停止消费者 timer、确认无进行中任务与旧 HTTP 连接，再停止旧主 API。排空证据 SHA256 为269ea6d43fba93bd7efff04c64f4ab6a9b863db37cc7e139a8db198dae6475f3。

正式清单 /mnt/data-disk/ai-cover-material-source/frozen-manifest.json 共916张：1479共838、3360共45、2373共33、979共0；截图171、YouTube745。两条旧业务拒绝排除，无身份/时间待确认问题。原三张 pending 已核实旧正式 ID7694349/7694348/7694350并进入正式清单。sources SHA256=1488f3d1a6da2570fab8ffa6b3abc08f3359dabbaa239db71976c670ca17f639；完整文件SHA256=30294559ff54c43a49a2bc2772483f8318f61f0a70160fb811f716f02feb4245。

HOLD、临时 Nginx 拦截及 cron 锁已解除，YouTube和日报timer均恢复。有限补传服务 ai-cover-source-backfill-20261020.service 从原顺序继续，每轮≤50逐张提交，自动跳过6张已确认代表样本；进度在 backfill-progress.json，逐图状态以 upload.sqlite3 为准。日报修后真实6样本复验通过：4张Dramawave纳入、2张其他产品按原报表范围排除，8个正式ID合并为4个图片版本，4条迁移不增加首次生成数，逐图请求国家正确，unresolved为空，history.json哈希前后不变；没有重发历史日报。只有916张及全部清单问题均对账完成，才能报告完整补传完成。
