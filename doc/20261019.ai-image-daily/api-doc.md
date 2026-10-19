# 接口

无新业务API。report.py参数--state --env-file --source-db --sync-db [--date YYYY-MM-DD]，完整过去自然日且不得早于已发布最新日，同日成功skip。--restore-public <state/backups/run> 校验当前hash后恢复旧页，无旧页不删除COS。

固定key codex-artifacts/html/dramawave-ai-image-daily/index.html；不可变快照daily/YYYY-MM-DD/index.html；text/html UTF-8与no-cache,max-age=0,must-revalidate。

内部schema_version=1，report_date/generated_at/timezone/currency/snapshot_kind/period/freshness/summary/comparisons/trend/groups/materials/unresolved/validation/notes。ID字符串，比例百分数，零分母null。CSV按筛选结果生成。
