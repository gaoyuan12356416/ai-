# 2026-10-26 日报账单口径上线记录

2026-10-26 11:51:06（北京时间）已完成 CPU 独立日报切换和读回。
分支 `codex/social-daily-bills-20261026`，运行代码提交
`2c44e0a2f07385844a5388bb215ab7b56d3994dc` 已先推送 GitHub，服务器从 GitHub
拉取完整 SHA 后创建干净 checkout，再执行 stage/activate。

## 生效范围

- 收入：`kunlunads_dev.ads_drama_bills`，UTC 当日 `af_purchase` 金额。
- 付费人数：各统计范围内正金额支付的 `user_id` 去重人数。
- 归属、发布成功门槛、手动短链范围、退款/激活/点击/访问/发布/Post/Repost、
  卡片渲染、原群、每日北京时间 11:00、发送锁和幂等账本均保留。
- 网页接口和主 API 未部署；其源码哈希和业务服务 PID 读回均与切换前相同。

## 验证证据

本地 `test_social*.py` 共 183 项，2 项平台相关跳过；CPU 同套 183 项全部通过。
编译与 `git diff --check` 通过。

用已确认的冻结账单和全局发布目录回放，再用预发布 release 的正式兼容入口
`scripts/youtube_publisher_daily_report.py --date 2026-10-25 --preview` 读取真实数据。
两次卡片 JSON 完全一致，均得到以下结果：

| UTC 统计日 | 收入 USD | 付费人数 | 支付订单 | 激活 | 成功发布 | 点击 | 访问 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-25 | 2,159.88 | 130 | 147 | 1,664 | 349 | 16,860 | 29,115 |

所有人员和渠道均匹配确认数据；冻结回放除财务字段、派生值、收入排序及财务来源说明外，
完整报告字段不变。真实预览的非财务指标也与原报告逐人、逐渠道一致。
原报告、纠正版、回执、正式及纠正投递库共 181 个文件 SHA256 前后相同；
正式投递库在线备份 `PRAGMA quick_check=ok`。此次上线验证没有发送飞书消息。

服务器审计目录：
`/mnt/data-disk/youtube-publisher-daily-report/audits/billing-deployment-20261026-2c44e0a/`
包含 `before.json`、`server-tests.log`、`frozen-replay-verification.json`、
`live-verification.json`、`deployment-readback.json` 和预览产物。

确认快照的 summary SHA256：
`a5fa84f363ceed0df02f5cdee57fc80853e18a9f82472c2898ca000424857cf9`。

## 当前部署与回滚

当前链接：`/opt/youtube-publisher-daily-report/current`

当前 release：
`/mnt/data-disk/youtube-publisher-daily-report/releases/2c44e0a2f07385844a5388bb215ab7b56d3994dc-owner-card`

旧 release / 回滚点：
`/mnt/data-disk/youtube-publisher-daily-report/releases/cedb6334cf9d5e9c2c28a96b067629668fb26d52-owner-card`

源码 checkout：
`/mnt/data-disk/youtube-publisher-daily-report/sources/2c44e0a2f07385844a5388bb215ab7b56d3994dc`

备份及部署清单：
`/mnt/data-disk/youtube-publisher-daily-report/backups/20261026T034809Z-owner-card/deployment.json`

仅切换以下三个文件，其他运行文件保留原版本：

| 文件 | SHA256 |
| --- | --- |
| `features/social_daily_report/billing.py` | `532db80a9f24fac99e87b73d905d3b37c08256827f6458f5ed5855a6bd0349ed` |
| `features/social_daily_report/collector.py` | `239dbab7daaac31d5a6bd4adf1580c0fa635b3af7e085985e7194d316eeb9173` |
| `features/social_analytics/routes.py` | `c0c1c8d71f2365095e53142bef43b299053264c6408349e337d5f59a7eb63e40` |

`youtube-publisher-daily-report.timer` 保持 active，下一次执行为
**2026-10-27 11:00:00 CST**。服务为 oneshot，下次执行读取新链接，无需重启 timer。

必要时在 CPU 执行以下精确回滚命令，脚本会检查当前链接和旧代码哈希并持有原发送锁：

```bash
python3 /mnt/data-disk/youtube-publisher-daily-report/sources/2c44e0a2f07385844a5388bb215ab7b56d3994dc/scripts/deploy_social_daily_billing.py \
  --rollback /mnt/data-disk/youtube-publisher-daily-report/backups/20261026T034809Z-owner-card/deployment.json
```

回滚仅恢复代码链接，不能恢复旧投递库；否则可能丢失已发送状态。旧版本恢复的是汇总表
财务口径，因此仅作为紧急代码恢复点，不代表新的账单统计口径。
