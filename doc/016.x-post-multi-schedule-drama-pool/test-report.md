# 测试报告

## 测试结论

离线回归和生产基线合并回归通过，0 失败、0 阻塞；可以进入生产部署验证。

## 测试范围

- 多账号多时间配置、冻结认领和恢复。
- 素材 FIFO、全局排重和既有发布合同。
- 短剧源表审计、剧/集顺序、固定文案和失败状态机。
- 容量、权限、页面缓存、sidecar/API 合同和 SQLite 增量迁移。
- 既有 X OAuth、daily、catch-up、GPU 修复、短链和日志回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 聚焦 store/runner/selector/UI/API 合同 | 70 | 70 | 0 | 0 |
| 生产基线合并后完整 `test_x*.py` | 291 | 291 | 0 | 0 |
| TT source-cache Python 回归 | 104 | 103 | 0 | 0（1 项 Windows 预期跳过） |
| TT bridge Node 断言 | 53 | 53 | 0 | 0 |
| Python 编译文件 | 14 | 14 | 0 | 0 |
| HTML 内联脚本 | 2 | 2 | 0 | 0 |
| navigation JSON | 1 | 1 | 0 | 0 |
| `git diff --check` | 1 | 1 | 0 | 0 |

## 缺陷情况

- BUG-001：相邻时间点和跨午夜冻结批次可能丢失，已修复并回归。
- SA/代码评审发现的权限、容量、审计、短剧失败回写和 UI 字段问题均已关闭。

## 验证证据

- 合并现网多时段排期 release `194fc42` 后，`test_x*.py` 291/291 通过，耗时 18.833 秒，其中新增短剧可投放时间边界、多端最晚值和缺失/非法 fail-closed 用例全部通过。
- TT source-cache Python 104 项通过（1 项仅在 Linux 验证 POSIX mode，Windows 按预期跳过）；Node 53 个断言通过。
- UI 专项：7/7 通过；两个页面各 1 段内联脚本解析成功。
- 所有本次变更 Python 文件 `py_compile` 成功。
- `static/navigation.json` 可解析。
- `git diff --check` 退出码 0，仅有 Windows LF/CRLF 提示。
- 生产源表最近 1000 行抽样中，app_id 1479 有 407 行，16 行描述含换行；描述空白归一化用例已覆盖。

## 遗留风险

- 生产登录态、systemd timer、现网导航合并和自然时间点尚未验证。
- 不在部署验证中主动创建 X Post；实际自然发布由用户配置的未来时间点触发。

## 发布建议

生产按备份、不可变 release、旧 timer mask、新 timer enable、浏览器验证顺序发布。
