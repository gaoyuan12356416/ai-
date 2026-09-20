# 2026-09-20 生产部署记录

代码提交：`de1d24a9534f789c5dcb374c58dafe5d86187cd0`，已 push 到 `gaoyuan12356416/ai-` 的 `codex/youtube-publisher-daily-report-20260920`，CPU 从 GitHub fetch 同一 SHA 部署。

服务器：`43.166.187.96`；代码指针 `/opt/youtube-publisher-daily-report/current` 指向 `/mnt/data-disk/youtube-publisher-daily-report/releases/de1d24a9534f789c5dcb374c58dafe5d86187cd0`。数据盘 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8` 已核验。源代码、数据、归档和备份在数据盘。

服务 `youtube-publisher-daily-report.service` 与定时器 `youtube-publisher-daily-report.timer` 独立部署。每天北京时间 11:00 发送到用户指定群；发布量和效果统一 UTC 日，效果包括全部历史内容。

## 验收证据

- 本地及 CPU：22 个报表/归因/日期/状态/发送去重测试、10 个既有 Post 日报投递回归通过。按用户回复调整 UTC 后，22 个相关用例再次通过。
- `systemd-analyze verify` 通过。生产同等沙箱（ProtectSystem=strict、ProtectHome=read-only、主 SQLite 文件只读）预览成功，约 2.7 秒。
- 2026-09-19 UTC 预览：发布 21，收入 $58.80，激活 94，点击 626，访问 925。59 个源表归因标识全部匹配，待归属 0。原始 `ads_drama_bills` 的 UTC event_time 聚合验证为安装 94、6 次购买合计 $58.80，与日报效果源一致。
- 其中黄晶晶发布 4、收入 $58.80、激活 63、点击 387、访问 564；曹新宇发布 15、激活 4、点击 31、访问 71；苏斯琪发布 2、激活 27、点击 208、访问 290。朱锦茵和郜远本统计日均为 0。此为部署时快照，正式发送时重新采集。
- 曝光字段没有有效 YouTube 日采集证据，明确显示未接入。收入/激活记录 channel 可为空，已经通过冻结 campaign 对关联原发布人而完整纳入。
- 首次自然触发 2026-09-20 11:00:00 北京时间，11:00:04 完成，Feishu `code=0`，消息 ID `om_x100b65c08df170a4c19dad81a56eea0`。消息 API 回读目标群正确、interactive、deleted=false，UTC 日期和主要指标正确。正式收入和其他合计与预览一致。回执和回读证据归档到 `reports/2026-09-19/{receipt,readback}.json`。
- 11:00:31 再次执行相同 service 返回 `already_sent`，投递表 `status=sent, attempts=1`，未重复发群。定时器下一次 2026-09-21 11:00:00 北京时间。
- 主 API PID 2668168、YouTube auto worker PID 2955771、统一 writer PID 2937249 保持。原本 inactive 的 legacy worker 保持 inactive。既有 Post 日报 timer 保持 active。未执行 YouTube 上传、发布、首评或重试。

维护技能的 YouTube 参考上下文已同步增加独立日报口径及定位信息；技能目录既有未提交内容不纳入本次代码提交。

## 回滚

停止后续日报：

```bash
systemctl disable --now youtube-publisher-daily-report.timer
```

恢复到本次新功能安装前（移除独立 unit 和代码指针，保留投递账本及归档）：

```bash
python3 /mnt/data-disk/youtube-publisher-daily-report/releases/de1d24a9534f789c5dcb374c58dafe5d86187cd0/scripts/install_youtube_publisher_daily_report.py --rollback /mnt/data-disk/youtube-publisher-daily-report/backups/20260920T025252Z
```

UTC 配置调整前的中间备份为 `/mnt/data-disk/youtube-publisher-daily-report/backups/20260920T025409Z`。正常回滚用上面的首次安装前备份，避免退回未确认的混合时区口径。
