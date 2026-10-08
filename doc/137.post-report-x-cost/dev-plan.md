# 实施计划
X随机配额从整日审计复算，失败传播data_available=false；独立Decimal成本模块按实际时间/Post ID去重，月度CLI复用；普通/汇总卡成本展示；修正版CLI默认TT兼容，增加X；独立安装器校验哈希，在线备份、锁定日报，仅切报告代码和unit。
验证：python -m unittest discover -s tests -p 'test_post_daily_report*.py'；python -m unittest discover -s tests -p 'test_x_post_cost_report.py'；python -m compileall -q features/post_daily_report scripts/post_daily_report_correction.py scripts/x_post_cost_report.py；git diff --check。
