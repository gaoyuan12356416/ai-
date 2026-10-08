# 统计契约
X渠道cost：status=estimated|partial、currency=USD、estimated_usd（字符串或null）、known_estimated_usd、display_usd、counts、breakdown_usd、source_counts、unpriced_posts、unconfirmed_attempts、unreadable_sources、rate_snapshot_date、pricing_url、note与daily。明确非实扣账单。
月报：python3 scripts/x_post_cost_report.py --month YYYY-MM。
修正版：python3 scripts/post_daily_report_correction.py --channel X --date YYYY-MM-DD --revision <稳定版本> --preview；授权后才用--send。
