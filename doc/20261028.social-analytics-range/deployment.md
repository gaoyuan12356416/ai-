# 生产验收与回滚

- GitHub 分支：`codex/social-analytics-range-20261028`；部署提交：`9b0c4ada1db0b5753788d55f48d827855914003f`。
- CPU：`43.166.187.96`；运行目录：`/root/drama_material_service`。服务器从 GitHub fetch，候选 checkout 为 `/mnt/data-disk/social-analytics/releases/9b0c4ada1db0b5753788d55f48d827855914003f`。
- 只替换 `features/social_analytics/scoped_metrics.py`，只重启 `drama-material-api.service`。PID `1046288`，active、NRestarts=0；健康接口 200，匿名报表仍为 401。独立发布服务 PID 和日报 release 未变。
- 本地社媒回归运行 213 项：211 通过，2 跳过；服务器查询专项 20 项全部通过；语法及 diff 检查通过。
- 线上 HTTPS 原条件（UTC 2026-10-01 至 2026-10-27、YouTube、全员、按日期分组）返回 200，耗时 30.904 秒，`quality.complete=true`，缺失日期为空。CSV 返回 200，1.803 秒，包含 27 个日期；各数值合计与 API 相同。
- 独立对账读取不带候选 campaign 谓词的全站点分日汇总，共 7,273 行，再按既有 AI 范围和归属规则汇总；27 天每一日及全部指标合计均与线上 API 相同，CSV 收入与退款也逐分核对一致。
- 此次快照：发布 545、链接 1,021、点击 72,376、访问 100,058、安装 9,715、付费转化 1,307、收入 $17,719.08、退款 $113.58。延续网页既有指标口径，不修改独立日报账单来源；后续回传可改变数值。
- 私有证据：`/mnt/data-disk/social-analytics/validation/youtube-range-20261028/`。不提交管理员会话、用户目录和原始业务明细到 Git。

备份：`/mnt/data-disk/social-analytics/backups/20261028T075146Z-web-scope-9b0c4ada1db0`。

精确回滚，在 CPU 执行：

```bash
cd /mnt/data-disk/social-analytics/releases/9b0c4ada1db0b5753788d55f48d827855914003f
python3 scripts/deploy_social_query_range.py rollback --backup /mnt/data-disk/social-analytics/backups/20261028T075146Z-web-scope-9b0c4ada1db0
```

部署器核对目标文件与备份摘要后恢复代码并重启主 API；保留全部数据库、发布结果和日报账本。
