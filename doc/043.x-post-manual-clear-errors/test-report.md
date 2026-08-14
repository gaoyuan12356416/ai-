# 测试报告

## 测试结论

本地功能与回归测试通过；生产零发帖验收待部署后补录。

## 测试范围

源数据原因拆分、修复结果明细、手动 run 归一、UI 中文展示、X 发布核心回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 专项测试 | 59 | 59 | 0 | 0 |
| X 全量命名回归 | 410 | 409 | 0 | 0（条件跳过1） |
| 核心附加回归 | 107 | 107 | 0 | 0 |

## 缺陷情况

BUG-001 已修复；生产验收通过后关闭。

## 验证证据

- `python -m unittest ...`：59/59。
- `python -m unittest discover -s scripts -p "test_x_post*.py"`：410 项，OK，skip=1。
- `test_x_posts.py`、`test_x_post_daily.py`、`test_x_post_ledger.py`：35/35、60/60、12/12。
- 全程 fixture/mock，无 X 写请求。

## 遗留风险

历史粗粒度任务不能补写素材 ID；页面仅做兼容映射，不修改审计历史。

## 发布建议

允许按 GitHub-first 流程部署；必须先备份，GPU/CPU 同 commit，且只用自然 `no_pending` 与账本不变验收。
