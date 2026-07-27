# 测试报告

## 测试结论

本地回归与生产验收均通过。账号列表状态列已在第 2 列首屏显示，
六账号补发子批次为 6/6 completed，失败和 unknown 均为 0。

## 测试范围

- 补发子批次 SQLite 迁移、父/子隔离、事务差集、FIFO、排重、聚合与日志。
- Sidecar daily-bearer-only 路由、服务端账号范围和发布资格门禁。
- runner 日期/数量/原因硬门禁、冻结计划恢复、全量预检、限流/未知结果停发。
- 账号列表第二列、12 列结构、导航版本、HTML 禁缓存和手工 oneshot。
- 既有账号授权、daily runner、发布 ledger、素材池和应用路由回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| `test_x*.py` | 229 | 229 | 0 | 0 |
| Python 编译 | 4 个受影响入口 | 4 | 0 | 0 |
| JSON / Node / diff 静态检查 | 3 | 3 | 0 | 0 |
| 生产验收 | 1 | 1 | 0 | 0 |

## 缺陷情况

BUG-001 已由生产浏览器关闭。生产验收发现 BUG-002：主后台日志 API
仍加载旧版共享 service，导致补发日志显示“批次 —”；已安装同一 Git
release 中的新版 service、窄重启主 API，并由真实浏览器确认六行均显示
“补发批次 1”。`failed_preflight` 终态语义 P2 继续按本次 fail-closed
策略执行。

## 验证证据

- `python -X utf8 -m unittest discover -s scripts -p "test_x*.py"`：
  `Ran 229 tests ... OK`。
- `python -X utf8 -m py_compile ...`：通过。
- `node --check static/quick-nav.js`、`navigation.json` 解析、
  `git diff --check`：通过。
- 生产 release：`5c8fc4f1f6d6a7d31e340c89e80ae373d8dbb73a`。
- 父批次 4 与部署前在线备份逐字段完全一致；行摘要为
  `5516b0abb9221a1fa97dee8c0e4c218db125119c4407aca52397efdc753083cf`。
- 子批次 1：账号 5–10、队列/日志 20–25，6/6 published，
  `failed=0`、`unknown=0`；全库 unknown/post_creating 为 0。
- 六个 X 预览 URL 与六个 `/s2l/20.html` 至 `/s2l/25.html`
  均返回 HTTP 200；短链追踪参数中的日志 ID、素材 ID 与 ledger 一致。
- 三个编码不合规素材 `5424607`、`5779194`、`5342752`
  经 GPU 修复并回传 COS 后发布。
- 真实浏览器：账号列表第 2 列显示 9 个“已配置”、1 个“未配置”；
  日志页存在六条“补发批次 1”且预览/短链入口齐全。
- SQLite `integrity_check=ok`；素材、账号/日期和 X Post ID 重复数均为 0。

## 遗留风险

- 真实 X API 仍可能限流或返回不确定结果；runner 会停止剩余队列并禁止盲重试。
- 发布后有 17 个未绑定候选，其中 15 个最近校验通过、2 个已明确标记
  不可用；当前足够覆盖次日 9 账号，但仍应持续补池。

## 发布建议

本次一次性补发已完成，不得再次启动 catch-up oneshot。保留现有 daily
timer；下一次自然运行是 `2026-07-28 10:00 CST`。
