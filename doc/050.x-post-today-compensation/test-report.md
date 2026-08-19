# 测试报告

## 测试结论

通过。新增功能与 X 全量自动化回归无失败。

## 测试范围

drama 范围补偿事务/CLI、schedule runner/store、全部 X 模块测试。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 新增聚焦测试 | 8 | 8 | 0 | 0 |
| 关键 schedule 回归 | 100 | 100 | 0 | 0 |
| 素材语种 FIFO 聚焦 | 2 | 2 | 0 | 0 |
| schedule 修复容量 | 2 | 2 | 0 | 0 |
| 正常时段让路恢复 | 1 | 1 | 0 | 0 |
| X 全量回归 | 707 | 705 | 0 | 0（2 跳过） |

## 缺陷情况

代码评审发现并修复 BUG-001（计划 UPDATE 未校验 rowcount）；无未关闭缺陷。

## 验证证据

- `python -m py_compile ...`：通过。
- `python -m unittest scripts.test_x_post_schedule_drama_scope_compensate -v`：8/8 通过，含范围补偿子 run 精确关联放行与未关联失败关闭。
- 关键 schedule 回归：100/100 通过。
- 素材无目标语种与 FIFO 重放测试：2/2 通过。
- schedule 专用修复上限优先于 daily 回退值，且部署示例固化为 17：2/2 通过。
- 素材预检为正常 drama 时段让路后，只能经精确 corrective 审计链恢复：1/1 通过。
- `python -m unittest discover -s scripts -p "test_x*.py"`：707 tests，OK，2 skipped。
- 生产 Python 兼容性：测试辅助代码不使用 Python 3.10+ 的
  `TemporaryDirectory(ignore_cleanup_errors=...)` 参数。

## 遗留风险

生产补偿仍需在部署 commit、实时账本、账号与库存无漂移条件下 validate-only 后执行。

## 发布建议

允许按 GitHub-first、备份、immutable release、自然 scheduler 验收流程发布。
