# 开发与验证

1. 共用features.cover_material_source：字段规范、图片字节验证、只读人员/catalog、SQLite出站、单图HTTP、正式记录校验。
2. 主API仅替换上传适配；YT保持COS/审核/发布资格，调用共用模块。
3. ops/ai-cover-custom-source/history.py从真实旧回执构建固定168小时计划；manage.py显式manifest/key执行、恢复、导出。
4. 日报添加新账本分支及跨新旧ID身份、独立汇总守恒。
5. 本地及CPU Python3.9测试、GitHub精确版本、真实代表样本、窄部署及补传。

验证命令：python scripts/test_cover_material_source.py；python ops/ai-cover-custom-source/test_history.py（如存在）；python ops/youtube-cover-material-sync/test_sync.py；python -m unittest discover -s ops/ai-image-daily -p test_report.py；node ops/ai-image-daily/ui_test.cjs；语法编译及git diff --check。
