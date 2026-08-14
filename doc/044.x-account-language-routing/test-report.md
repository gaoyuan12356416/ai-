# 测试报告

## 测试结论

通过。代码已按 GitHub-first 流程部署到生产，并完成无真实发帖的只读、离线和自然调度验证。

## 测试范围

语言路由新增用例，以及全部 `test_x*.py` 账号、OAuth、素材池、短剧池、X Auto、队列、调度、恢复、中继和 UI 契约回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 跳过 |
| --- | --- | --- | --- | --- |
| Python X 相关测试 | 663 | 661 | 0 | 2 |
| 编译/语法检查 | 1 | 1 | 0 | 0 |
| diff whitespace 检查 | 1 | 1 | 0 | 0 |

## 缺陷情况

- BUG-001：历史 `jp` 与标准 `ja` 不一致；已通过兼容规范层修复并回归。
- 开发回归中发现并修复历史队列冻结标记、并发绑定窗口、非标准语言兼容、Premium 适配器签名、静态缓存版本和 Windows SQLite 文件句柄问题。

## 验证证据

```text
python -m unittest discover -s scripts -p 'test_x*.py'
Ran 663 tests
OK (skipped=2)
```

```text
python -m compileall ...
Exit code: 0
git diff --check
Exit code: 0
```

## 遗留风险

- 当前预览没有符合条件的素材，返回 `x_auto_no_eligible_material`；这是素材条件结果，不是语言路由故障。后续首个自然到期任务仍应结合冻结任务快照和台账复核。
- 回滚不得直接用旧 SQLite 覆盖已经产生新发布历史的在线数据库；新增列可由旧代码安全忽略。

## 发布建议

生产 release `7ed5f203e028129f88afbc675da9237326bfd364` 已完成部署。17 个账号语言、254 条历史队列兼容投影、两个 SQLite `quick_check`/外键检查、三个服务、相关 timer、主应用/静态文件哈希和 X Auto 健康门禁均通过核对；自然观察期间 X Post 与 X Auto 台账均无增长，未知结果为 0，未创建真实 X Post。
