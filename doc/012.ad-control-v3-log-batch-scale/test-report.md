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

## 生产验证

- 真实线上基线：`f55be78cf5366913528116f5f63ad104fb5b9572`；部署 runtime：`a4dad6d2ff708b04a434945b5c18e9f6caf2fdef`。
- 生产 staging 使用数据盘 `TMPDIR`，191 tests、Python 编译、JS 语法和 deployer `--check` 全部通过。
- 5 个二级索引均由 reader 回读列顺序；Preview 配对子查询由全表扫描变为 `ref + Using index`。
- DDL 和代码发布前后均为 execution 32 行、execution_target 5462 行，未修改或删除审计数据。
- 已登录生产浏览器：逻辑总数 25，第一页 20；计划正式执行行显示“预检已合并”，详情时间线显示“预检并锁定候选 → 完成”；手动试算仍单独显示。
- `drama-material-api.service` 与 `ad-control-v3-runner.timer` 均为 active；恢复后连续自然 tick 为 `groups=0, meta_writes=0, failed=0`。
