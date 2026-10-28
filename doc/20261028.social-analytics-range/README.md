# YouTube 多日查询超时修复

社媒效果分析选择 2026-10-01 至 2026-10-27、YouTube、全员、按日期分组时，发布数为 545、链接数为 1,021，但效果指标为空。线上复现发现 2,038 个冻结 campaign ID 被拆成 43 次查询；第 12 次即耗尽 40 秒请求预算。源数据实际覆盖全部 27 天。

多日且超过 100 个候选 ID 的 YouTube 查询改用现有 `(site_id, dt)` 索引，每次最多七天、3,000 个 ID；原条件只需五次查询。SQL 仍在聚合前过滤精确大小写 campaign ID，保持全目录归属冲突检查、人员权限、成功发布时间和手动短链规则。保留八秒单条 SQL、40 秒总预算、只读 63350、FIFO Gate、缺失日期及任何批次失败不缓存部分结果。少量 ID、单日、X、Facebook 和独立日报保持原查询计划。

仅加大原 campaign 索引批次已验证会触发 MySQL 3024；仅复用连接仍不足以完成原范围。新站点日期计划的只读预演在 26.2 秒完成，27 天完整。实际部署后的 HTTP、CSV 和独立来源对账另记验收文件。

## 验证与部署

`python -m unittest discover -s tests -p 'test_social*.py'` 验证权限、归属、每日汇总、分段边界、跨批去重、末批失败、缺失日期和 CSV。

提交推送 GitHub 后，在 CPU 数据盘 checkout 精确提交；执行：

```bash
python3 scripts/deploy_social_query_range.py stage
python3 scripts/deploy_social_query_range.py activate --backup <stage 返回路径>
```

只替换主 API 的 `features/social_analytics/scoped_metrics.py` 并重启 `drama-material-api.service`。备份及摘要保护由既有部署器负责；发布服务、归属账本、独立日报保持原状。

精确回滚：

```bash
python3 scripts/deploy_social_query_range.py rollback --backup <同一备份路径>
```

回滚检查当前文件摘要，恢复本次代码备份并窄重启主 API；不恢复任何数据库或历史投递文件。
