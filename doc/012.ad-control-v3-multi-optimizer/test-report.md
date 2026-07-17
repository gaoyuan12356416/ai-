# 测试报告

## 测试结论

本地测试通过；生产数据库迁移与王鹏只读冒烟待发布步骤完成后补录。

## 测试范围

V3 身份、规则服务、存储、权限、路由、动态 UI、暂停/复制执行、UTC+8、导航和精确 overlay 部署器。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Python unittest | 169 | 169 | 0 | 0 |
| 动态 UI 函数契约 | 31 | 31 | 0 | 0 |
| 语法/差异检查 | 3 | 3 | 0 | 0 |
| 生产冒烟 | 2 | 0 | 0 | 2 |

## 缺陷情况

- BUG-001：本地已修复，待线上验证关闭。
- 代码评审中发现新鲜安装回滚遗漏关系表，已同步修复并增加静态测试。

## 验证证据

- `python -m unittest`：169/169；其中精确部署器 15/15，耗时 246.877 秒。
- `tests.test_ad_control_v3_core`：64/64。
- `tests.test_ad_control_v3_repository`：22/22。
- `tests.test_ad_control_v3_live_execution`：14/14。
- `tests.test_ad_control_v3_ui` 零参数契约：31/31。
- `python -m py_compile`、`node --check`、`git diff --check`：通过。

## 遗留风险

- 生产关联表尚未创建；代码不能先于迁移发布。
- 线上只验证 API 身份和列表，不会用真实广告验证 Meta 写操作。

## 发布建议

满足 GitHub-first、数据盘 checkpoint、先迁移后代码和王鹏会话脱敏冒烟后发布。
