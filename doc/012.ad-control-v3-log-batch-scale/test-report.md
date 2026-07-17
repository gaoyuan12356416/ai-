# 测试报告

## 本地

- 定向 repository/routes/UI：62 tests，OK。
- core/copy-time/permissions/repository/routes：110 tests，OK。
- live execution：13 tests，OK。
- deploy + navigation deploy：28 tests，OK，耗时约 252 秒。
- UI + usability：39 tests，OK。
- 合并真实线上基线 `f55be78` 后，完整 `test_ad_control_v3*.py`：191 tests，OK，耗时 226.055 秒。
- Python 3.9 兼容编译、`node --check`、`git diff --check`：通过。

## 生产只读基线

- `ad_control_v3_execution`：32 行；`ad_control_v3_execution_target`：5462 行。
- `ad_control_v3_preview`：24 行；`ad_control_v3_preview_target`：5424 行。
- 最大单 Preview/Execution 对象数：2607。
- 现有成对计划记录应用合并查询后：逻辑批次 25，第一页返回 20。
- 发布前执行计划显示管理员默认列表为全表扫描、Preview 配对子查询无可用索引，证明本次索引迁移有必要。

## 待发布验证

- 精确 commit staging/deployer。
- DDL 回读与发布后 `EXPLAIN`。
- 动态页面真实浏览器验证。
- 数据行数前后不变校验。
