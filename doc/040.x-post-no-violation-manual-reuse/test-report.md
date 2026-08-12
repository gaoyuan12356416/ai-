# 040 测试报告

## 部署前状态

- Python 编译：`features/x_posts/service.py`、`selector.py`、daily/manual runner 通过。
- JavaScript 语法：`node --check static/quick-nav.js` 通过。
- 定向回归：5 个受影响模块共 109 项通过。
- 全量 X 回归：`python -m unittest discover -s scripts -p "test_x_post*.py"`，392 项通过、1 项按既有环境门禁跳过。
- 生产备份副本迁移：待记录。
- 生产部署：待记录精确 commit、release、backup、账本前后计数与自然 timer 证据。

验收期间禁止创建真实 X Post。
