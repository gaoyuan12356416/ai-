# 测试报告

## 测试结论

本地、服务器精确提交回归与生产只读验收均通过；功能已从 GitHub 精确提交 `9e6c5c899f1c849e62e22f5c01496a4fb983f256` 发布。

## 测试范围

见 `test-cases.md`。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| V3 合并后最终精确回归 | 181 | 181 | 0 | 0 |
| 新增专项最终运行 | 14 | 14 | 0 | 0 |
| 扩展旧版/共享回归 | 322 | 319 | 0 | 3 |
| 编译/语法/差异检查 | 4 | 4 | 0 | 0 |

## 缺陷情况

当前无本次变更引入的已确认缺陷。

## 验证证据

- 固定时钟产生 `[*copybyAI*07171455]`。
- 五种 copy carrier 拓扑验证所有新对象同后缀、复用父对象不改名。
- created_data 写入参数使用 Meta 回读的三级名称。
- 名称回读失败保留新对象 ID、quarantine intent，ledger/activation 均未调用。
- UTC+8 SQL 边界、API 响应头、递归时间字段、非中国浏览器时区和 runner stdout 均通过。
- `app.py` 相对最终发布 source `3bcf083` 零差异；线上新增 scheduler 修复已合并保留。
- 服务器在精确目标提交上执行 V3 回归 181/181 通过，证据为 `/mnt/data-disk/ai-ad-control-v3/test-runs/9e6c5c8/unittest-v3.log`，SHA-256 为 `9f9443c083b23ef85ad2963d579d46f854aef9e7a55b2617b8031c5beb7c5b1c`。
- 生产 overlay 后重复检查为 `unchanged`；API 服务与 runner timer 均为 active，配置和共享 `app.py` 未变化。
- 自然 timer 运行输出 `ran_at=2026-07-17T16:09:03.598205+08:00`、`display_timezone=UTC+8`，本次 tick 为 0 次 Meta 写。
- 已登录生产浏览器验证执行日志页：明确标记 UTC+8、正常加载 21 条记录、控制台无 error/warn。

## 遗留风险

- 本地完整共享 `app.py` 导入依赖既有 `features.x_accounts`，当前 Git 工作树缺少该目录，导致 3 个旧测试无法启动；该文件不在本次变更和部署覆盖范围，服务器完整环境需复核。
- 未在生产主动制造新的 ACTIVE Meta 对象；真实命名以首次受控复制后的回读和 lineage 为最终证据。

## 发布建议

已按 exact overlay、数据盘备份和生产健康门禁发布。未为本次命名变更主动制造新的 Meta 对象；下一次正常受控复制需继续以 Meta 名称回读和 lineage 作为真实平台证据。

## 生产发布记录

- 发布 source：`3bcf0839de78f481ea299abf9acf64db2cb8d61c`。
- 发布 target：`9e6c5c899f1c849e62e22f5c01496a4fb983f256`。
- 完整备份：`/mnt/data-disk/ai-ad-control-v3/backups/predeploy-copy-name-utc8-20260717T160635+0800-3bcf083`。
- 精确 overlay 备份：上述目录下 `exact-overlay/ad-control-v3-3bcf0839de78-to-9e6c5c899f1c`。
- SQLite 在线备份 `PRAGMA integrity_check=ok`，SHA-256 为 `0ff01864045bc86d6e43a0b56fb19afcb7f31e91642d1165c0da84f872bc00a9`。
