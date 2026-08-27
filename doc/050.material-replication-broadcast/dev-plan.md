# 开发计划

状态：**已上线；真实飞书送达未试发验收**。批准范围见 [requirements.md](requirements.md)，开发基线 `ee6e00c`，上线代码 `0a391260f6de1d2e99b351b21d41a613866a5cfb`。实际隔离回归、线上非发送检查与真实送达验收分别记录，不把代码部署等同真实消息送达。

## 开发范围

在 `codex/material-replication-broadcast-20260827` 上新增独立复刻播报 POST 接口、专属 Token、严格批次验证、持久化 outbox 与异步投递；保留旧 `/material-task-status-events` 全部合同及重试行为。

上游每 2 小时汇总发起、每 1 小时汇总失败，负责跨批次只推新增。接收端每次一个剪辑师/一个事件类型，可靠接收后尽快发出，不实现额外定时汇总、资源 20 分钟提醒、素材业务表轮询或真实复刻生成。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态与门槛 |
| --- | --- | --- | --- |
| 确认业务边界及接口合同 | 主执行者 / 文档负责人 | `requirements.md`、`api-doc.md`、`api-doc.html`、`openapi.json`、`examples/` | 已完成；OpenAPI 1.0.0，已同步上线状态和真实送达验收边界 |
| 需求/方案评审 | 独立评审负责人 | `sa-review.md` | 按实际评审记录，不以计划代替结论 |
| 严格验证与固定消息模板 | 主执行者 | `features/material_replication_broadcast/` | 两事件分支、NFC/trim、顺序及双字节上限 |
| 独立持久化与投递状态机 | 主执行者 | 新功能 outbox | 原子接收、唯一键、恢复、冻结 UUID、未知结果有限重试 |
| 精确 HTTP 接入与配置 | 主执行者 | `app.py`、`.env.example` | 专属 Token，无旧路由/Token/重试侵入 |
| 用例与代码评审 | 独立 QA / 评审负责人 | `test-cases.md`、`sa-test-review.md`、`sa-code-review.md` | 显式覆盖并发、尺寸、映射、未知结果和旧接口回归 |
| 本地编译与模拟测试 | 主执行者 / 独立 QA | `scripts/test_material_replication_*.py` | 所有外部投递用 fake/mock；不发送真实消息 |
| 发布与回滚准备 | 主执行者 | `deploy.md` | GitHub 精确版本、先备份、旧接口隔离、新旧 outbox 保留 |
| 最终回归及发布建议 | 独立 QA | `test-report.md` | 记录实际命令、结果、剩余风险；线上验收另列 |

## 实施顺序

1. 核对基线/工作区，只修改批准范围；复核现有身份映射和飞书调用的可复用边界。
2. 先完成无网络的验证、规范化、正文格式与最终 JSON 字节预算；再实现独立 outbox 的唯一键、持久化和恢复。
3. 接入新精确路由和 Token 校验；同键同内容读取原批次当前状态，变更数组顺序同样触发 409。
4. 实现目标/正文/UUID 冻结、私聊/明确失败兜底、3300 秒内最多 5 次尝试与 `delivery_unknown` 终止；不改变旧发送策略。
5. 并行完成测试/评审/接口文档；发现问题逐项修正并复测，最终回归由独立负责人统一记录。
6. 本次已按部署文档完成上线与非发送检查；后续发布仍须核验配置和回滚材料。代码已部署不等于实际飞书送达，首个获准业务批次仍需独立验收。

## 编译 / 构建命令

本项目是 Python 服务，不使用 Maven。以下保留可复现的验证入口，在仓库根目录执行；本轮 CPU 隔离全回归为 244/244 PASS，具体测试选择、命令和输出以 [test-report.md](test-report.md) 的实际记录为准：

```powershell
python -m py_compile app.py features/material_replication_broadcast/service.py features/material_replication_broadcast/delivery.py
python -m unittest scripts.test_material_replication_broadcast scripts.test_material_replication_webhook_app
python -m unittest scripts.test_material_status_broadcast scripts.test_material_status_webhook_app
git diff --check
```

文档结构与 JSON 解析命令：

```powershell
node -e "const fs=require('node:fs'); for(const p of ['openapi.json','examples/started.json','examples/failed.json']) { JSON.parse(fs.readFileSync('doc/050.material-replication-broadcast/'+p,'utf8')); console.log(p+': JSON.parse PASS'); }"
```

回归范围覆盖：HTTP 202/400/401/409/413/415/422/503；Unicode/NFC、空收件人、全部数组边界；终端状态重复读取；同键并发；进程重启；有效映射与明确失败兜底；含引号/反斜杠/中文的最终 JSON 字节预算；未知投递 3300 秒/5 次上限；已冻结目标和正文不变；旧接口仍可正常独立运行。`202` 入队和发送行为由隔离 Mock 回归验证，生产未创建真实测试批次或飞书试发；静态 JSON 解析也不能代替完整 OpenAPI 规范校验或实际送达验收。

## 风险与依赖

- 上游须持久化幂等键及原内容；超时/5xx 使用原键原内容按 1、5、30、120 秒重试。已 202 接收不得换键补发；跨批次只推新增由上游保证。
- 身份只经已有精确映射链解析，不引入 caller-supplied 邮箱/open_id。公开文档不得嵌入既有群 ID、内部数据库地址、真实 Token。
- 已配置的专属 Token 仅在服务端以 root 所有、0600 权限保管，通过安全渠道提供给获准调用方；不随文档交付。
- 128 KiB 限制包括最终请求 JSON 与兜底包装；上游可在 32 KiB 请求体以内仍触发最终消息超限，须自行拆为新子批次，接收端不截断。
- 3300 秒/5 次是本功能安全边界，不能据此承诺外部消息严格 exactly-once；超期未知结果必须保持未知，不能自动新键/新 UUID/改目标兜底。
- 新旧 outbox 应分别受控且可恢复；回滚代码不能回滚或清空已经接收的投递事实。
- 本地开发与验证不授权部署、生产状态变更、真实飞书消息或素材/复刻任务。

## 完成记录

2026-08-27：以 `ee6e00c` 为开发基线完成实现和接口合同，上线代码为 `0a391260f6de1d2e99b351b21d41a613866a5cfb`。发布执行者记录：CPU Python 3.9.6 隔离全回归 244/244 PASS、外网请求 0、耗时 16.792 秒；18:00:03 +08:00 主 API active，PID 1123265。

内网回环与公网 HTTPS 各完成新接口无 Token 401、合法 Token 空 items 422、超过 32 KiB 413、旧接口无 Token 401，共 8/8 PASS。新 outbox 已创建且为 0 条；没有真实测试批次或飞书试发。真实送达仍需首个获准业务批次验收。实际测试证据见 [test-report.md](test-report.md)，部署及回滚证据见 [deploy.md](deploy.md)。
