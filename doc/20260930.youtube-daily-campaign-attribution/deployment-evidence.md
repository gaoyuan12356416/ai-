# 日报更正上线结果

2026-09-30 14:05 北京时间，2026-09-29 UTC 更正版已发送并通过 Feishu 消息查询回读。群沿用原配置 oc_7c683c5770aef2e6c84a456e52cad389，原消息仍保留。

## 数据核对

| 发布人 | 已公开视频 | 收入 USD | 激活 | 落地页点击 | 落地页访问 |
|---|---:|---:|---:|---:|---:|
| 苏斯琪 | 7 | 275.56 | 98 | 647 | 919 |
| 黄晶晶 | 2 | 45.71 | 28 | 229 | 378 |
| 曹新宇 | 1 | 22.01 | 12 | 72 | 124 |
| 朱锦茵 | 0 | 0.00 | 14 | 77 | 109 |
| 彭放 | 0 | 0.00 | 0 | 0 | 4 |
| 郜远 | 0 | 0.00 | 0 | 0 | 0 |
| 合计 | 10 | 343.28 | 152 | 1025 | 1534 |

退款 0，充值人数 27。源数据最大更新时间 2026-09-30 05:30:00 UTC（北京时间 13:30），更正版生成于 06:05:18 UTC（北京时间 14:05:18）。全部指标与 AI 同版聚合及只读源数据守恒；待归属 0 条/0 分。

旧版合计收入 20936 分，单列待归属 13392 分。按 campaign ID 补入短链 317 的 12093 分、318 的 1299 分，均归苏斯琪。其他手动短链补入点击、访问和激活；生成短链没有增加发布量。

## GitHub 与部署

- 分支：codex/youtube-daily-campaign-attribution-20260930。
- 运行代码：dd5798606429fe1458b8e8a4af571e4d35ac6b2e，已推送 GitHub，服务器从 GitHub 拉取精确 SHA。
- 服务器：43.166.187.96。
- 入口：/opt/youtube-publisher-daily-report/current。
- Release：/mnt/data-disk/youtube-publisher-daily-report/releases/dd5798606429fe1458b8e8a4af571e4d35ac6b2e。
- 旧版：de1d24a9534f789c5dcb374c58dafe5d86187cd0。
- 备份：/mnt/data-disk/youtube-publisher-daily-report/backups/20260930T060505Z。
- 原发送账本快照及审计：/mnt/data-disk/youtube-publisher-daily-report/audits/campaign-id-v2-20260930/。
- 共享 analytics/report.py SHA-256：5143768de7d854e6049f5830b87a71362f16d0b4238a91e40fdd7f4fcd40ac17，与 AI 线上实现完全相同。

只切换独立日报 release 并 daemon-reload。timer active，下次 2026-10-01 11:00 Asia/Shanghai。主 API、YouTube 自动发布 worker、统一 writer 的 PID/ActiveState/NRestarts 前后相同，无重启。未更改源统计、历史链接、频道归属或业务数据库。

## 验证与投递

- Windows 和 Linux 分别 50/50 项通过，py_compile、git diff --check 通过，独立审查无阻断。
- 服务器真实 --preview 成功，金额和数量与此前只读源数据及 AI 聚合相同。
- 更正版消息：om_x100b64fe27312cb4df35bd0be7ce342；Feishu code=0，GET 消息核实接收群、日期和 $343.28 正文，未删除。
- 更正版 ledger：sent，attempts=1，UUID 2e1d5a4f-c0bb-56b8-8b14-7dbc2abea626。
- 再次运行同日期/版本返回 already_sent，无再次发送。
- 原 report/card/preview/receipt 四文件 SHA-256 及原发送账本记录逐项不变。
- 更正版归档：/mnt/data-disk/youtube-publisher-daily-report/corrections/campaign-id-v2/reports/2026-09-29/。
- 技能上下文已更新：ai-backend-maintenance/references/youtube-auto-publish.md。

## 精确回滚

```bash
python3 /mnt/data-disk/youtube-publisher-daily-report/releases/dd5798606429fe1458b8e8a4af571e4d35ac6b2e/scripts/install_youtube_publisher_daily_report.py --rollback /mnt/data-disk/youtube-publisher-daily-report/backups/20260930T060505Z
```

回滚恢复原 release 和定时配置，保留现有发送账本及报表，不撤回或重放已发送消息。
