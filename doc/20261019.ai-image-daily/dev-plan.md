# 开发执行

collector.py负责只读来源、应用和一致日快照，analysis.py负责Decimal汇总和守恒，report.py负责磁盘/锁/恢复/COS，render.py与report.html负责单文件页面。

按标准流程并行UI及部署器，主开发实现数据和恢复测试。分支codex/ai-image-daily-20261019，先GitHub push、服务器fetch精确SHA、git archive release并逐blob验证，主业务不重启。

验证：python -B -m unittest discover -s ops/ai-image-daily -p '*test*.py' -q；Python3.9 AST；git diff --check；Linux同测试、systemd verify、真实数据/COS hash/浏览器/重复执行。结果见test-report.md。
