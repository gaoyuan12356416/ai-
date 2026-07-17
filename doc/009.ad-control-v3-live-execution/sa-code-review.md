# SA 代码评审

## 结论

本地评审和生产 Canary 均通过，可以进入受控业务验收。

## 评审范围

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 | 高 | `_copy_tree` | 中途异常时必须知道已创建 ID | 创建响应拿到 ID 立即写入可变隔离状态 | 已修复 |
| CR-02 | 高 | Graph paging | `paging.next` 可能含 Token | 只复用 after cursor，禁止记录 next URL | 已修复 |
| CR-03 | 高 | quota lock | 跨用户可并发复制同一来源 | 同时获取用户日锁和全局来源锁 | 已修复 |
| CR-04 | 中 | execute | DB 目标列表读取有 200 行上限 | 先按 preview summary 检查 50 个硬上限 | 已修复 |
| CR-05 | 中 | created_data product | insight 枚举与发布 product 不同 | 以账户 + Meta 对象 ID 连接，app_id 解 Token | 已修复 |
| CR-06 | 高 | copy ledger | Meta ISO 时间不能直接写入 MySQL DATETIME | 入库前转为无时区 MySQL wall-clock；保留原 intent 恢复，禁止重复制 | 已修复 |
| CR-07 | 中 | V3 顶部能力提示 | 线上开关已开放但 UI 仍显示“自动调度尚未发布” | 根据 `/meta` 权限动态渲染 observe/live/disabled 三态 | 已修复 |

## 编译 / 验证结果

- Python compile：通过。
- JavaScript syntax：通过。
- V3 unittest：154/154 通过，修复后完整执行两次。
- V2/共享调控定向回归：115/115 通过。
- `git diff --check`：通过（仅 Windows LF 提示）。
- 生产真实 PAUSED copy/pause、幂等恢复、schema、UI 和连续 runner tick：通过。
