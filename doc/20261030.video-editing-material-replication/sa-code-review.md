# 独立代码评审

日期：2026-10-30。评审者：reserved_ui_review（独立于后端与前端实现代理）。范围：material_replication.py、routes/service/store 增量、worker/unit、正式前端复刻流程、主代理历史导入 CLI。部署脚本由本代理实现，须由主代理复核，不将自查标为独立评审。

## 结论

截至本次核对，未发现剩余的阻断级权限、出站状态或精确幂等问题。后端整套测试与真实 CPU 验证仍是发布前条件。

- 所有 GET/POST 进入既有模块/同源/任务对象授权；普通成员归属由服务端决定，不能通过修改前端字段越权。
- 先登记 submitting 后出站；崩溃恢复只转 unknown_outcome；HTTP 无重定向、无自动 POST 重试；独立 lease 阻止多 worker 同时消费。
- 操作 ID 校验参数摘要；B 精确去重键独立处理，保护 pending/submitting/accepted/partial/unknown_outcome，明确拒绝允许新操作。
- 复用旧 B 记录时展示冻结参数及冲突；跨任务引用走操作映射和权限检查，历史能找到复用记录。
- worker 使用真实协议 token 请求头；主 API 不读取凭据。错误与保存响应有 secret/URL 脱敏。
- 导入 CLI 默认只读 DB 且不 import app；--apply 仅调用 import_history，校验审计哈希和素材身份，归属覆盖须有匹配成功回执，不排入 pending。

## 已修复发现

| 严重度 | 位置 | 问题与修复 | 状态 |
|---|---|---|---|
| P1 | worker 请求头 | 主代理根据真实联调证据指出初版错误使用 Bearer；已改为 token 自定义头，并核对两条出站分支 | 代码已核对；生产请求未重新发送 |
| P2 | for_task | 初版每 3 秒读取每个任务全部历史；已改 COUNT + 单条摘要，完整历史仅显式 GET | 代码已核对 |
| P2 | replicationRecordHtml | Unix 秒直显；已转 zh-CN 日期时间 | 代码已核对 |
| P2 | 关闭/重开未决提交 | 新表单按需求清空，但原冻结参数不可被误解；pending 区域现完整展示旧请求，awaiting 禁止重放，仅未知请求可沿原 ID 重放 | 代码已核对；前端代理回报测试通过 |

## 部署自查（非独立评审）

发现并修复：unit 尚未安装时失败恢复不能因 disable 不存在的 unit 而中断；移除 marker 后 manifest 写入丢回执可再核对；链接备份目录拒绝；回滚不恢复 SQLite。14 项部署单元测试通过。

主代理已独立复核最终部署脚本：限定文件清单、GitHub SHA 检查、原文件散列、SQLite 在线备份与增量迁移演练、故障恢复、旧 worker PID 保持和账本保留策略均符合方案；独立重跑 14 项部署测试通过。CPU 数据盘 UUID、空间和现有两个 worker 状态已只读预检，发布后再核对真实 manifest。
