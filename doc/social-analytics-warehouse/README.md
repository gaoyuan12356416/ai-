# 社媒效果分析数仓

原网页每次筛选、排序或导出会重新读发布目录并查询效果源表。2026-11-04 的昨天全渠道样本耗时 44.885 秒。本实现将效果、链接归属和发布事实拆开保存到已授权的 `101.32.56.53/ads_ai`，页面读取同版本的只读本地服务副本，完全不触发源表 SQL。

## 表及口径

- `social_analytics_youtube_daily`、`social_analytics_x_daily`、`social_analytics_facebook_daily`：UTC 日期、站点、大小写精确 campaign ID 日效果；收入/退款为整数美分。
- `social_analytics_link_catalog`：冻结的链接归属及维度，含冲突证据。
- `social_analytics_publication`：发布证据和链接目录键。一个发布对象可能具有多条候选归属，不能直接 COUNT(*) 当成成功发布量；沿用公共 report 引擎按发布 ID 去重并检查归属冲突。
- `social_analytics_refresh_runs`：版本、状态、覆盖日期、数据质量及发布清单。

Facebook 先按 Page/素材/campaign 精确匹配再聚合；YouTube 保留原有 32 字符历史别名规则。所有 HTTP 请求仍先执行原模块权限，再按租户及人员筛选。独立日报和发布服务保持原入口。归属冲突、缺失日期和不可用来源继续在报表中提示。

## 同步与一致性

每小时第 05 分运行一次。默认从现有账本最早的有效日期开始，重算全历史至 UTC 今天，接收历史修正；每条源 SQL 最多四天、20 秒，同一批共享 FIFO 门控读连接，每批最多 28 天。源库和入仓验证只读 63350，写入只用 63353 的六张专用表。部署时显式执行仓库 DDL，定时任务不执行 DDL。

数据先写新版本，随后经 63350 核对每表行数、记录键/载荷校验值以及所有效果指标合计，成功后才原子替换 `current.json`。网页不读取正在写入的版本。源查询失败、写失败或副本验证失败保留旧页面快照。页面显示最近同步时间，超过两小时提示延迟。保留当前和上一个仓库版本；清理仅针对本报表已识别的旧版本。

历史起点之前的日期没有入仓证明，不按零统计。单次网页请求仍支持原有最长 93 天范围。英文剧名属于可降级的说明字段，不阻断效果更新。

## 部署

1. 提交并推送 GitHub；服务器检出精确提交到 `/mnt/data-disk/social-analytics-warehouse/repo`，`/opt/social-analytics-warehouse/current` 指向它。
2. 确认数据盘 UUID 为 `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`。备份路径为该数据目录下 `backups/`。
3. 复核 `deploy/social-analytics-warehouse.sql`，运行 `python3 -m features.social_analytics.warehouse_refresh --install-schema`。
4. 在独立 oneshot 中运行 `python3 -m features.social_analytics.warehouse_refresh`，完成首次全量入仓，核验新旧查询、权限和 CSV。
5. `python3 scripts/deploy_social_warehouse.py install` 只替换报表模块和两份页面静态文件，添加独立 API 环境 drop-in，重启 `drama-material-api.service`，启用小时 timer。

凭据来自服务器现有环境文件。可用独立的 `SOCIAL_ANALYTICS_WAREHOUSE_WRITE_MYSQL_{HOST,PORT,USER,PASSWORD}` 覆盖 writer；默认复用现有 `AD_CONTROL_ACTION_LOG_MYSQL_*` 凭据，固定写端口 63353 和 ads_ai 库。通用 `ADMIN_MAPPING_MYSQL_*` 始终保持 63350。

## 验证及回滚

本地：`python -c "import sys,unittest;sys.path.insert(0,'tests');r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests',pattern='test_social*.py'));sys.exit(not r.wasSuccessful())"`，以及 `node --check static/social-analytics.js`。

回滚：`python3 /opt/social-analytics-warehouse/current/scripts/deploy_social_warehouse.py rollback --backup /mnt/data-disk/social-analytics-warehouse/backups/<timestamp>`。恢复原网页读路径并停用本功能定时器，保留仓库事实和本地快照；不删除业务源表，也不修改发布任务。
