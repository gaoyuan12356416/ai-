---
name: socalkite-effect-query
description: 查询 SocialKit 发布记录与社媒收入效果，使用已发布报表快照，或只读核对 social_short_link 与收入表的关联。用于平台、产品、账号、剧目、素材来源分析及归因缺口检查；不执行发布、建表、回填或定时任务变更。
---

# socalkite效果数据查询

## 默认入口

优先使用 AI 后台 `/socialkit-analytics.html` 或 CPU 服务器已经发布的本地快照。网页/API 需要 Feishu 登录及 `socialkit_analytics` 模块权限；管理员和获授权用户查看全部报表数据，不按个人归属推断权限。

本技能的只读入口复用已配置的 CPU SSH helper，不需要再次提供密码：

```powershell
python <skill-dir>/scripts/query_report.py --start 2026-10-19 --end 2026-11-01 --view effects --group-by platform
python <skill-dir>/scripts/query_report.py --view publications --group-by product,platform --filter product=Dramawave
```

省略日期时由报表模块选择已发布的最近 14 天。`--format csv` 导出同一查询结果；`--version` 固定已发布版本。CLI 不刷新源库、回填或修改数据库。保存输出时用当前任务的交付目录，不写入技能目录。

先查看返回的版本、日期范围、源更新时间、报表更新时间和未归属量。源查询失败后旧快照仍可用；明确说明数据截至时间，不把旧快照称为刚更新的数据。当前部署按北京时间每天 11:00 重算前 14 个日期，不做日内实时轮询。

## 分析口径

- 收入来自 `kunlunads_dev.ads_facebook_page_insight` 的 `channel='socialkit'`；按源表 UTC 日期，币种 USD。`views` 是落地页访问类指标，不是社媒视频播放量。
- 收入先按自身粒度聚合，以精确字符串 `campaign = social_short_link.short_code` 获取归属。短链主表是收入归属来源，不能用任务表中的短码覆盖范围替代它。
- 发布来自 `social_auto_publish_task`，排除 `deleted_at<>0`，日期使用账号当地 `schedule_date`；成功内容数为 `SUM(item_success)`，不是任务行数。`item_failed=0` 不等于全部计划成功，未知状态保持原值。
- 短链、任务和账号表的平台编号不同，使用 [schema-and-query.md](references/schema-and-query.md) 中的映射。账号使用团队、平台、外部账号 ID；剧目使用 App ID、content ID；名称只用于展示。
- 同一短链可能出现在多个任务和多个日期。发布与收入分别汇总，不将原始任务行直接连接到收入行；收入包含历史短链贡献，不计算“当日收入÷当日发布数”作为单帖收益。
- 金额按整数分或 Decimal 汇总。未归属键单列，并核对“已归属＋未归属＝源总额”。没有归属不能解释为对象零收入。
- 收入国家、设备系统只筛选效果数据；账号语言、账号国家是发布维度。跨日期充值人数为源表加总，不声称是去重人数或逐人漏斗。

## 源库核验

仅在快照缺失、口径核验或用户明确要求源数据时读取 [schema-and-query.md](references/schema-and-query.md)。连接路线使用 `server-43-166-187-96` 技能；保持凭据在 CPU 进程内，禁止查询或复制账号令牌字段。

本技能默认只读。源数据查询不改变任务状态，不修复收入、不启用刷新、不发送通知。若请求变成维护报表或部署，转入对应的后台维护与部署流程，不因调用查询技能而扩大写入范围。
