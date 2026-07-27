# 测试报告

## 测试结论

本地回归通过，尚待生产部署、真实浏览器与六账号实际补发验收。

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
| 生产验收 | 1 | 0 | 0 | 1 |

## 缺陷情况

BUG-001 已完成代码修复；待生产浏览器关闭。独立评审发现的日志批次
可观测性 P2 已修复；`failed_preflight` 终态语义 P2 已按本次 fail-closed
策略接受并写入需求。

## 验证证据

- `python -X utf8 -m unittest discover -s scripts -p "test_x*.py"`：
  `Ran 229 tests ... OK`。
- `python -X utf8 -m py_compile ...`：通过。
- `node --check static/quick-nav.js`、`navigation.json` 解析、
  `git diff --check`：通过。

## 遗留风险

- 真实 X API 仍可能限流或返回不确定结果；runner 会停止剩余队列并禁止盲重试。
- 六条发布后当前 14 条可用库存理论上只余 8 条，少于次日九账号需求；
  发布完成后必须回读实际剩余数并明确补池要求。

## 发布建议

完成独立 SA 代码复核后可按 GitHub-first 发布；必须先在线备份 SQLite、
保存非敏感 Token hash/mode 和精确 live 文件，再迁移、浏览器验收，最后手工
启动一次 catch-up oneshot。不得手工启动 daily service。
