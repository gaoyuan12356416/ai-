# 接口与报表约定

无 HTTP API 或数据库 schema 变更。

CLI：scripts/youtube_publisher_daily_report.py --date 2026-09-29 --correction campaign-id-v2 --preview|--send。

正常定时发送命令保持原样。更正版需明确日期，源效果不可用拒绝发送。归档位于 state/corrections/campaign-id-v2/{previews,reports}/日期，发送 ledger 为该版本目录的 delivery.sqlite3。

report.json schema_version=2；validation.attribution=unique_frozen_campaign_id_v2，rows 增加 tenant；matched_metrics 带冻结 campaign ID/link ID，unmatched_metrics 记录无法归属原因。
