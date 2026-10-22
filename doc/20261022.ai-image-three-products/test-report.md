# 测试与上线记录

2026-10-22，本地执行：
- python -B -m unittest discover -s ops/ai-image-daily -p 'test*.py' -q：105项通过。
- python -B ops/ai-image-daily/ui_test.py：8项通过。
- 全Python源文件ast.parse(feature_version=(3,9))：通过。
- node --check ops/ai-image-daily/report.js：通过。

包含真实形状三产品来源组合、跨产品金额/去重、迁移原图、应用过滤、匹配对照、空值与读失败、gzip/多对象恢复与回滚、缩略图边界、UI分包与错误状态。生产数据、桌面移动浏览器和最终性能结果尚待上线验证；本记录不代表已部署。

