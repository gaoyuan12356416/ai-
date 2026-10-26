# 社媒日报收入与付费人数改用订单账单

本次仅修改独立 11:00 社媒发布人日报的两个指标。`revenue_cents` 来自
`kunlunads_dev.ads_drama_bills` 的 `af_purchase.event_revenue_usd`；`conversions`
改为该统计范围内正金额支付的 `user_id` 去重人数，同一个用户多笔订单只计一人。
退款、激活、点击、访问、成功发布、Post/Repost、卡片结构、原群和时间不变。
网页接口继续使用原取数方式；部署只切换独立日报，不部署主 API。

统计日继续按 UTC 自然日，由 `event_time >= start AND event_time < end` 判断，
不能混入 X 账号运营页的北京时间口径。效果继续覆盖历史已确认发布内容，
并保留 YouTube 手动短链；未确认发布的 X/FB 任务不获得短链豁免。

归属和非财务指标共用一次只读发布账本快照：全局唯一 campaign ID，保留 YouTube
唯一 32 字符别名，Facebook 额外匹配冻结 Page/素材 ID。禁止按名称或 af_channel
补归属。所有 MySQL 查询复用 63350、`@@read_only=1`、SQL FIFO Gate、单次最多
20 秒、失败最多重读一次及总 120 秒预算。一次最多 50,000 笔账单，超限即失败。
只读取用户 ID 的 SHA256 用于进程内去重，不将用户标识写入日报。

人数在人员、人员渠道、渠道和总计分别去重，因此跨渠道/人员有交集时不能直接相加。
金额仍须严格守恒。账单查询失败、异常金额、重复账单 ID、正金额缺用户 ID，或
当天四个站点都没有支付记录时，财务指标为未知并禁止正式发送，不使用汇总表金额
兜底、不播报虚假的零。已有流量但某一渠道没有支付时，可以显示该渠道为零。

归档增加 `financial_source`、`quality.billing`、`shared_report.billing_scope_audit`；
原汇总表范围对账保留为 `shared_report.insight_scope_audit`。普通查询、发布队列、
历史报告、回执和投递状态不变。原发送锁与幂等逻辑继续生效，不重发已发送日期。

## 验证与上线

```powershell
python -m unittest discover -s tests -p 'test_social*.py' -q
python -m py_compile features/social_daily_report/billing.py features/social_daily_report/collector.py features/social_analytics/routes.py scripts/deploy_social_daily_billing.py
git diff --check
```

提交并推送 GitHub 后，CPU 从 GitHub 检出完整 SHA，运行：

```bash
python3 <clean-source>/scripts/deploy_social_daily_billing.py --sha <SHA>
python3 <staged-release>/scripts/social_publisher_daily_report.py --date 2026-10-25 --preview --state-dir <isolated-validation-dir>
python3 <clean-source>/scripts/deploy_social_daily_billing.py --activate <deployment.json>
```

先用已核对账单/归属冻结快照回放，再核对真实只读预览；只允许收入和付费人数以及
相应排序、派生付费率改变。已确认 2026-10-25 的验收基准为收入 $2,159.88、付费
130 人、147 笔支付；不改激活 1,664、发布 349、点击 16,860、访问 29,115。
发布前保留旧 release、正式投递库在线备份及 manifest。部署脚本沿用 run.lock，
验证线上基线和 GitHub 提交，服务必须 inactive；每日 timer 下次调用自动使用新代码，
无需重启主 API、媒体发布 worker 或 timer，不为验证额外发送飞书消息。

回滚只恢复代码链接，绝不恢复旧账本：

```bash
python3 <clean-source>/scripts/deploy_social_daily_billing.py --rollback <deployment.json>
```

回滚会恢复旧汇总表财务口径，应仅用于紧急代码恢复；保留全部历史发送记录。
