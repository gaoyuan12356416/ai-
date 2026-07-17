# 测试报告

## 测试结论

本地 204 项自动化全部通过；生产关系表迁移与首轮代码发布已完成。首轮只读验证发现王鹏、凌云柯当前均为临时 admin，原管理员分支只提供全量下拉、未返回本人别名范围；本次补丁已覆盖该场景，最终双用户只读冒烟待增量发布后补录。

## 测试范围

V3 身份、规则服务、存储、权限、路由、动态 UI、暂停/复制执行、UTC+8、导航和精确 overlay 部署器。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Python unittest（不含 UI 契约） | 172 | 172 | 0 | 0 |
| 动态 UI 函数契约 | 32 | 32 | 0 | 0 |
| 语法/差异检查 | 5 | 5 | 0 | 0 |
| 生产关系表迁移 | 4 | 4 | 0 | 0 |
| 最终生产身份冒烟 | 4 | 0 | 0 | 4 |

## 缺陷情况

- BUG-001：基础多别名链路已上线，待最终身份冒烟关闭。
- BUG-002：临时 admin 本人别名未扩展；本地已修复，待增量上线验证。
- 代码评审中发现新鲜安装回滚遗漏关系表，已同步修复并增加静态测试。

## 验证证据

- `python -m unittest discover -s tests -p "test_ad_control_v3*.py"`：204/204，耗时 225.566 秒。
- `tests.test_ad_control_v3_core`：67/67。
- `tests.test_ad_control_v3_repository`：22/22。
- `tests.test_ad_control_v3_live_execution`：14/14。
- `tests.test_ad_control_v3_ui` 零参数契约：32/32。
- `python -m py_compile`、`node --check`、`git diff --check`：通过。
- checkpoint 脚本 `bash -n` 通过；越界备份目录在任何写入前被拒绝。
- 生产 `ads_ai.ad_control_v3_rule_group_optimizer` 已建表，4 个现存规则组回填 4 行，无缺失；迁移重复执行保持 4/4。

## 遗留风险

- 管理员本人别名补丁尚待增量发布；发布前 V3 runner timer 保持停止。
- 线上只验证 API 身份和列表，不会用真实广告验证 Meta 写操作。

## 发布建议

满足 GitHub-first、数据盘 checkpoint、服务端完整回归和王鹏/凌云柯会话脱敏冒烟后恢复 runner timer。
