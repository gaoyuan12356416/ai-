# SA 代码评审

## 结论

2026-08-27 独立评审结论：本地影响范围通过，无未关闭 P0/P1 阻断项；最终 CPU Python 3.9.6 同范围及无消息上线检查已由发布负责人完成并回传。代码评审本身不作为真实飞书送达证明。

## 评审范围

`app.py` 增量、`features/material_replication_broadcast/service.py` / `delivery.py`、部署脚本、三份新专项及旧接口与相关主 API 合同。独立 QA 仅新增测试与评审文档，业务修复由主负责人 / 服务负责人完成。

已确认：新旧入口 Bearer Token 分离；仅共享飞书应用 tenant token 与底层发送能力。新表、UUID 命名空间、正文和投递阶段隔离；无素材/复刻业务写入、接收端小时聚合或跨批次去重。旧行为保留，仅增加 certainty 元数据。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 / BUG-001 | P1 | app.py / send_material_status_feishu_text | 内部鉴权重试丢失较早未知结果，可能错误兜底 | 累计 send_outcome_uncertain，旧 retryable/refresh/fallback 不变 | 已修复；两项独立故障注入及旧专项通过 |
| CR-02 / BUG-002 | P1 | scripts/deploy_material_replication.py / rollback | 部分发布时 app 尚为原版本，原回滚 guard 拒绝恢复 | 只接受 original/deployed 两个已知 SHA；保留后续漂移阻断 | 已修复；部分发布回滚 Mock 通过 |
| CR-03 / BUG-003 | P1 | scripts/deploy_material_replication.py / rollback | 常规回滚停服务前缺少活动任务检查 | 与 apply 共用 restart_safe，并在 app 切换前再次检查 | 已修复；活动队列时零 systemctl 调用 |
| CR-04 / BUG-004 | P1 | scripts/deploy_material_replication.py / probe | 带专属 Token 的精确验证可能跟随重定向 | NoRedirect；校验 HTTP 和错误码；关闭 HTTPError 资源 | 已修复；本机 302 验证无后续请求 / Token 转发 |
| CR-05 | P2 | delivery.py / handle_request | 超长数字 Content-Length 的 int 转换可产生非结构化异常 | 转换前检查长度；非法请求零入库 | 已验证结构化 400 |
| CR-06 | P1 | enqueue / handle_request | 已接受批次不可因后续 renderer 变化而不能幂等读取 | 尺寸预检仅对新键执行，历史重放不重新渲染 | 已验证原批次 202、零重渲染 |
| CR-07 / BUG-005 | P1 | scripts/deploy_material_replication.py / verify | 缺少新 outbox 表被当作空表，可能误签 worker 初始化成功 | 先要求表存在，再校验为空；缺表不得写验证证明 | 主负责人发现并修复；两类缺表快照独立回归通过 |

## 编译 / 验证结果

- `python scripts/test_material_replication_safety.py --full-regression`：最终 **244/244 PASS**，18.619 秒，失败 / 错误 / 跳过 / 外网尝试均为 0。
- 独立安全专项 24 项：包括 3299/3300/3301 秒窗口、旧租约拒绝发送、后台 worker 在 POST 成功后落库失败的安全恢复、阶段预算重置和真实 requests 序列化字节数。
- 8 个本轮 Python 文件 `py_compile` 通过；主模块、新服务、部署脚本及全部 11 个回归脚本按 Python 3.9 grammar 解析通过。实际本地运行时为 Python 3.14.3，不冒充 CPU 3.9 实跑。
- 部署脚本只读审查与本地 Mock 通过：精确 Git SHA / live baseline、在线 SQLite 备份与同快照演练、配置备份、配置漂移保护、拒绝-only 探针、缺表阻断且零证明写入、窄服务切换、数据库不回滚。
- 详细命令、模块计数、源文件指纹和生产验证 / 未验边界见 `test-report.md`。既有套件中的 Mock HTTP、故障注入错误日志不代表生产异常。

## 生产验证补录

发布负责人对最终提交 `0a391260f6de1d2e99b351b21d41a613866a5cfb` 回传：CPU Python 3.9.6 同范围 244/244 PASS，16.792 秒，外网尝试 0；apply、备份 quick_check / 建表副本演练、8 个拒绝-only 探针通过，新表存在且 0 条，主 API active。完整日志与 verification.json 位置见 `test-report.md`。本节未追加代码修改，未把无消息上线验证等同真实送达；首个获准业务批次仍待验。
