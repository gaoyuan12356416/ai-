# YouTube 发布人日报

每天北京时间 11:00 向群 `oc_7c683c5770aef2e6c84a456e52cad389` 发送独立飞书卡片。用户确认口径为“全部内容的昨日效果 + 昨日发布量”，并明确选择发布量和效果统一按 UTC 日期统计。

发布量取 AI 后台 `drama_youtube_publish`，按 `operator_user_id` 分组，显示发布人姓名。仅计已确认公开的视频，按 `video_id` 去重；首评或同步失败不能抹去已确认的视频发布事实。排队、上传、预约未公开、视频未知不算成功。内部部署 canary 排除。

效果取只读副本 `101.32.56.53:63350` 的 `kunlunads_dev.ads_facebook_page_insight`，限定 `site_id='2284'`、统计日，使用 `(site_id,dt)` 索引。虽然历史表名含 Facebook，该表实际保存 YouTube W2A 归因数据。使用冻结短链中完整的 `(af_c_id,c)` 与效果表 `(campaign_id,campaign)` 精确匹配，归属到原发布账本的操作人；不按素材上传人或可为空的 channel 字段分组。历史 `ai_youtube` 同样通过唯一的 campaign_id 关联。归因不唯一时单列待归属，禁止分摊或静默丢失；所有分组与源表总额守恒。

- 发布量和效果统一按源表昨日 UTC 日期，等于北京时间当日 08:00–次日 08:00。源表只有天粒度，不能假称精确的北京时间自然日。生产 unit 显式固定 `--publication-timezone UTC`。
- 收入：归因内购金额，USD，退款另列；激活：安装事件数；点击/访问：落地页 clicks/views。
- impressions 字段存在但尚无有效 YouTube 日曝光采集证据，卡片显示“未接入”，不能把全零字段或 views 当成曝光。
- 查询成功且当日源表有记录时，无匹配效果的人员展示 0；无源记录/查询失败展示“待核实”。数据截至本次生成，后续回传可能补齐。

## 独立执行与验收

`scripts/youtube_publisher_daily_report.py --preview` 默认不发送。`--send` 每统计日/群只发送一次，使用独立状态库和 `youtube-publisher-daily-report` UUID 命名空间，避免与 10 点 Post 日报碰撞。已发送直接返回 `already_sent`；发送中/结果未知禁止盲目重试。预览和正式归档分开，保留 report、card、receipt 与发送账本。

服务器所有数据、归档、备份、Git checkout 位于 `/mnt/data-disk/youtube-publisher-daily-report`。只读 SQLite（包含当前 WAL）与受 FIFO Gate 限流的只读 MySQL；禁止绕过 Gate。不调用 YouTube 发布、重试、首评或授权接口，不初始化发布 Store，不重启发布 worker。

本地测试：`python -m unittest discover -s tests -p test_youtube_daily_report.py`；共享投递回归：`python -m unittest discover -s tests -p test_post_daily_report.py`。

GitHub 推送精确 SHA 后服务器 fetch/check out，执行 `python3 scripts/install_youtube_publisher_daily_report.py --sha <SHA>`。先通过同等 systemd 沙箱的 `--preview`，确认数据与安全权限，再启用 `youtube-publisher-daily-report.timer`。过 11 点后可通过 `systemctl start youtube-publisher-daily-report.service` 发当天正式首报，回读飞书并重复执行验证 `already_sent`。

回滚：`python3 <release>/scripts/install_youtube_publisher_daily_report.py --rollback <backup>`，恢复原 unit/pointer/timer 状态，保留全部数据和发送回执。首次安装回滚停用并移除这两个独立 unit；既有 Post 日报和 YouTube 发布不受影响。
