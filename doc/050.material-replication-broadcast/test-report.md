# 测试报告

## 测试结论

2026-08-27，独立最终 QA：**本地影响范围 244/244 PASS**；0 失败、0 错误、0 跳过、0 外网尝试。最终版本 CPU Python 3.9.6 同范围也为 244/244 PASS，发布负责人已完成无消息上线验证；真实业务送达仍待获准批次验收。

最终集合耗时 18.619 秒，执行于 Windows / Python 3.14.3。基线 `ee6e00c000c31a538b9294a9da7f084dd9e5f9ac`，工作分支 `codex/material-replication-broadcast-20260827`。这是按改动确定的 11 个模块完整回归，不宣称仓库全部其他套件均已执行。生产上线证据与本地自动回归分开记录，见下方服务器验证节。

## 测试范围

新接口 / 两类消息 / 专属入口 Token / 精确映射 / 严格整批校验 / 幂等 / 独立表 / 冻结目标正文 UUID / sticky uncertainty / 3300 秒安全窗口 / 租约 / 兜底 / 后台恢复；旧接口全专项与 X、TT、FB、Drama Synthesis 主 API 影响面；部署回滚安全与对外样例一致性。

## 执行统计

| 模块 | 数量 | 通过 | 失败 / 错误 / 跳过 |
| --- | ---: | ---: | --- |
| 新 pure service / outbox | 46 | 46 | 0 / 0 / 0 |
| 新 HTTP / runtime | 22 | 22 | 0 / 0 / 0 |
| 独立 safety / deployment / docs | 24 | 24 | 0 / 0 / 0 |
| 旧 material status pure | 15 | 15 | 0 / 0 / 0 |
| 旧 material status HTTP | 18 | 18 | 0 / 0 / 0 |
| X accounts app contract | 30 | 30 | 0 / 0 / 0 |
| TT posts app contract | 15 | 15 | 0 / 0 / 0 |
| TT auto app contract | 11 | 11 | 0 / 0 / 0 |
| X auto app contract | 10 | 10 | 0 / 0 / 0 |
| FB auto app contract | 5 | 5 | 0 / 0 / 0 |
| Drama synthesis upgrade | 48 | 48 | 0 / 0 / 0 |
| 合计 | 244 | 244 | 0 / 0 / 0 |

## 缺陷情况

BUG-001 至 BUG-005 均经主负责人修复并通过独立回归；没有未关闭的 P0/P1 代码阻断项。修复前问题经静态控制流审查确认，不虚构未运行的历史失败测试统计。Content-Length 边界与历史幂等重放额外加固也已覆盖。BUG-005 由主负责人在发布前发现：新表缺失不能等同空表；补充独立测试验证空快照 / 仅旧表快照均报错，零验证文件写入、零成功输出。

最终注入 `OperationalError` 的测试故意让第一次成功响应落库失败，真实后台 worker 随后以相同目标 / 正文 / UUID 恢复并确认私聊；该日志是测试预期。

## 验证证据

最终可复现入口（工作目录为仓库根）：

```bash
python scripts/test_material_replication_safety.py --full-regression
python -m py_compile app.py features/material_replication_broadcast/__init__.py features/material_replication_broadcast/service.py features/material_replication_broadcast/delivery.py scripts/deploy_material_replication.py scripts/test_material_replication_broadcast.py scripts/test_material_replication_webhook_app.py scripts/test_material_replication_safety.py
git diff --check
```

回归入口列明上述 11 个模块，并对 DNS、socket connect / connect_ex 安装进程级非 loopback 网络阻断；所有业务发送均 Mock。末尾实际统计：

```json
{"tests":244,"failures":0,"errors":0,"skipped":0,"external_network_attempts":0}
```

8 个 Python 文件编译退出码 0。Python 3.9 grammar 检查覆盖 app、新服务 / runtime、部署脚本与全部 11 个测试脚本；grammar 检查只是预检，CPU 3.9.6 实跑另见服务器验证节。独立 QA 的本地回归未发送真实飞书消息、未调用生产 API、未读取生产数据库或实际执行部署 / 回滚；上线操作与拒绝探针由发布负责人执行。

最终影响范围回归时记录的 Windows 工作副本 SHA-256（工作副本可能包含 CRLF；不是服务器 LF checkout 的文件指纹，不能直接跨平台比较）。源码若变更，须重新确定影响范围并复验；跨环境使用发布 commit / Git blob 对齐，服务器部署 manifest 另记真实 LF 文件 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| app.py | fcefd7686cc9335c4fc092ddc232a15ce947be245a7edd5f985ea3132d5a8743 |
| features/material_replication_broadcast/service.py | cf9547a3c1e6e08f9a3f2f50d27e5734f05520e8d935784c51714290b69eb41f |
| features/material_replication_broadcast/delivery.py | e99aff06a0e9d20c790cafbdb55a28972d51512a7d03c3c5742ab7c804917f6b |
| scripts/deploy_material_replication.py | 838fcdeea1c84c2d1a9a5d6528d7da48d256514b706f467e3979b597119e6c2b |
| scripts/test_material_replication_broadcast.py | 5b92f0265eb67cff546a85c965471c56a11d5e5e7904307f0e92ce3633ff66dd |
| scripts/test_material_replication_webhook_app.py | 44141cfa1b323e60181ecb9ded9273633cf3b1343be2b2f65b743c9bb1d10041 |
| scripts/test_material_replication_safety.py | a9879aeae434c6e93a26d9d7bbf077d684423ebc7db2e348b4e85914d5a58fdd |

原功能提交为 `9e2d5901428bf8d67ea218734785543ec8bed1b5`，最终补 fix 的发布提交为 `0a391260f6de1d2e99b351b21d41a613866a5cfb`。补 fix 仅部署验证脚本增加新 outbox 必须存在的门禁，并新增独立回归与部署说明；本报告 244 项结果覆盖此修复，API / service / runtime 业务代码未改变。不把原功能 SHA 或基线 SHA 冒充最终修复提交。

## CPU 原功能版本预验证

发布负责人回传：CPU / Python 3.9.6 对 `9e2d5901428bf8d67ea218734785543ec8bed1b5` 执行同一入口，243/243 PASS，17.032 秒，外网尝试 0；日志 `/mnt/data-disk/material-replication-broadcast/qa/preflight.4AeZc2/regression.log`。这是发布负责人执行的服务器证据，本独立 QA 未连接服务器。该记录仅保留为原版本历史；最终修复版本已单独执行 244 项，结果见下节，没有沿用旧 243 项结果签署新门禁。

## CPU 最终版本与无消息上线验证

以下服务器结果由发布负责人执行并回传，本独立 QA 未连接服务器、未自行调用生产 API。

- 最终发布 commit：`0a391260f6de1d2e99b351b21d41a613866a5cfb`。
- CPU / Python 3.9.6 执行 `python scripts/test_material_replication_safety.py --full-regression`：**244/244 PASS**，16.792 秒；失败 / 错误 / 跳过 / 外网尝试均为 0。日志：`/mnt/data-disk/material-replication-broadcast/qa/final.qOYRqR/regression.log`。
- `apply` 成功；备份目录：`/mnt/data-disk/material-replication-broadcast/backups/20260827-175953-pre-0a391260`。备份 SQLite `quick_check` 与同快照副本建表演练均通过；未执行生产数据库回滚。
- 主 API 为 active，实际变更前 / 后 PID 为 `1116402` / `1123265`。验证证明于 `2026-08-27T18:00:03+08:00` 保存至备份目录内的 `verification.json`；仓库留存证据为 `evidence/deployment-verification.json`。
- 新 `material_replication_broadcast_outbox` 表明确存在且为 0 条；旧 outbox 的 delivered 为 6410。没有入队有效测试批次，也没有真实飞书测试发送。
- 发布后只读复核：全部 deployed / backup / config 文件哈希匹配 manifest，旧 service SHA 未变；备份 `quick_check=ok`，备份目录权限 `0700`，新 Token 配置权限 `0600`。
- 启动日志 `18:00:01.059 +08:00` 显示 `material replication batch worker configured=True`，startup failure 为 0；主 API、`drama-material-job-worker` 与 nginx 均 active，`nginx -t` 通过。上述结果由发布负责人回传，独立 QA 已读取仓库留存证据并核对记录一致性。

loopback `http://127.0.0.1:8787` 与 HTTPS `https://ai.yingliangads.com` 各执行下列四项，共 **8/8 PASS**：

| 端点 / 请求 | 预期 HTTP | loopback | HTTPS |
| --- | ---: | --- | --- |
| 新端点，无凭据 | 401 | PASS | PASS |
| 新端点，有效专属凭据、空 items | 422 | PASS | PASS |
| 新端点，有效专属凭据、32769 字节请求体 | 413 | PASS | PASS |
| 旧端点，无凭据 | 401 | PASS | PASS |

这是服务部署、存储初始化和拒绝路径的无消息验证，不能证明实际制作人私聊或兜底群已真实收到消息。startup / 配置权限附加核对已完成；本轮部署检查无待补项。

## 遗留风险

- 最终版本 CPU 回归、备份 / 建表演练、apply、线上拒绝-only 探针及 startup / 配置权限核对均已通过。没有通过真实业务批次验证收件人映射或远端送达。
- 上游 2h/1h 聚合、跨批次只推新增及同失败不再走旧 API 是对接责任；接收端同键去重不能证明上游已履行。
- 没有真实飞书送达验收。冻结 UUID 的有限安全重放不承诺跨窗口或端到端严格 exactly-once；delivery_unknown 保持停发待对账。
- 部署 / 回滚必须保留新旧 outbox 和最新发送事实；有活动正式任务或源码 / 配置漂移时停止切换，不强行解除门禁。

## 发布建议

独立 QA 签署：本地代码与影响范围通过；发布负责人回传的最终 CPU 回归及无消息上线检查通过。保留首个获准真实业务批次的验收项：核对实际制作人、事件 / 条目正文及 outbox 与远端送达结果；上游聚合和跨批次去重责任同步确认。未获明确授权前，不为验收制造测试播报。
