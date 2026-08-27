# 测试报告

## 测试结论

2026-08-27，独立最终 QA：**本地影响范围 243/243 PASS**；0 失败、0 错误、0 跳过、0 外网尝试。允许提交 / 推送本次代码，并进入 CPU Python 3.9.6 同范围验证和受控发布。

最终集合耗时 21.455 秒，执行于 Windows / Python 3.14.3。基线 `ee6e00c000c31a538b9294a9da7f084dd9e5f9ac`，工作分支 `codex/material-replication-broadcast-20260827`。这是按改动确定的 11 个模块完整回归，不宣称仓库全部其他套件均已执行，也不表示生产已上线。

## 测试范围

新接口 / 两类消息 / 专属入口 Token / 精确映射 / 严格整批校验 / 幂等 / 独立表 / 冻结目标正文 UUID / sticky uncertainty / 3300 秒安全窗口 / 租约 / 兜底 / 后台恢复；旧接口全专项与 X、TT、FB、Drama Synthesis 主 API 影响面；部署回滚安全与对外样例一致性。

## 执行统计

| 模块 | 数量 | 通过 | 失败 / 错误 / 跳过 |
| --- | ---: | ---: | --- |
| 新 pure service / outbox | 46 | 46 | 0 / 0 / 0 |
| 新 HTTP / runtime | 22 | 22 | 0 / 0 / 0 |
| 独立 safety / deployment / docs | 23 | 23 | 0 / 0 / 0 |
| 旧 material status pure | 15 | 15 | 0 / 0 / 0 |
| 旧 material status HTTP | 18 | 18 | 0 / 0 / 0 |
| X accounts app contract | 30 | 30 | 0 / 0 / 0 |
| TT posts app contract | 15 | 15 | 0 / 0 / 0 |
| TT auto app contract | 11 | 11 | 0 / 0 / 0 |
| X auto app contract | 10 | 10 | 0 / 0 / 0 |
| FB auto app contract | 5 | 5 | 0 / 0 / 0 |
| Drama synthesis upgrade | 48 | 48 | 0 / 0 / 0 |
| 合计 | 243 | 243 | 0 / 0 / 0 |

## 缺陷情况

BUG-001 至 BUG-004 均经主负责人修复并通过独立回归；没有未关闭的 P0/P1 代码阻断项。修复前问题经静态控制流审查确认，不虚构未运行的历史失败测试统计。Content-Length 边界与历史幂等重放额外加固也已覆盖。

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
{"tests":243,"failures":0,"errors":0,"skipped":0,"external_network_attempts":0}
```

8 个 Python 文件编译退出码 0。Python 3.9 grammar 检查覆盖 app、新服务 / runtime、部署脚本与全部 11 个测试脚本；这是兼容性预检，不等于已在 Python 3.9.6 执行。未发送真实飞书消息，未调用生产 API，未读取生产数据库或实际执行部署 / 回滚。

最终功能回归开始时记录的 SHA-256（源码若变更，须重新确定影响范围并复验）：

| 文件 | SHA-256 |
| --- | --- |
| app.py | fcefd7686cc9335c4fc092ddc232a15ce947be245a7edd5f985ea3132d5a8743 |
| features/material_replication_broadcast/service.py | cf9547a3c1e6e08f9a3f2f50d27e5734f05520e8d935784c51714290b69eb41f |
| features/material_replication_broadcast/delivery.py | e99aff06a0e9d20c790cafbdb55a28972d51512a7d03c3c5742ab7c804917f6b |
| scripts/deploy_material_replication.py | f526c41745fd8a406e789a9085555dbce2f7499c4f8f16e0ec7b11069e004097 |
| scripts/test_material_replication_broadcast.py | 5b92f0265eb67cff546a85c965471c56a11d5e5e7904307f0e92ce3633ff66dd |
| scripts/test_material_replication_webhook_app.py | 44141cfa1b323e60181ecb9ded9273633cf3b1343be2b2f65b743c9bb1d10041 |
| scripts/test_material_replication_safety.py | 77e4513774aad612d3ce551abf9c262314cf7b4cf0257bce44abe6a94ad43ec3 |

功能回归后仅清理 `__init__.py` 多余末尾空行并更新评审文档；该文件未承担业务逻辑。提交 SHA 由主负责人在最终发布记录中补充，不把基线 SHA 冒充功能提交。

## 遗留风险

- CPU Python 3.9.6 实跑、服务器实际配置 / SQLite 备份、重启安全门禁和线上拒绝-only 401/422/413 探针尚由发布负责人验证，不能替代成本地 PASS。
- 上游 2h/1h 聚合、跨批次只推新增及同失败不再走旧 API 是对接责任；接收端同键去重不能证明上游已履行。
- 没有真实飞书送达验收。冻结 UUID 的有限安全重放不承诺跨窗口或端到端严格 exactly-once；delivery_unknown 保持停发待对账。
- 部署 / 回滚必须保留新旧 outbox 和最新发送事实；有活动正式任务或源码 / 配置漂移时停止切换，不强行解除门禁。

## 发布建议

独立 QA 签署：本地代码与已定义影响范围通过。可以推进 GitHub-first 发布准备；仅在 CPU 同范围通过、真实备份可用、活动任务门禁通过且源文件指纹与提交一致后执行窄范围发布。真实消息联调另行取得明确授权，不为验收制造测试播报。
