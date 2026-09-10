# 技术设计复核

结论：按以下约束实现可与旧流程共存。

| 事项 | 处理 |
|---|---|
| HK task-ID 缓存冲突 | 新旧使用同一 SQLite 发布 ID 序列，仅 workflow 分流 |
| 封面审核前发布 | 独立准备阶段没有上传账本；批准后才交接 |
| 同视频提前公开 | private 上传，thumbnail 与 processing 均成功后 public，再 readback |
| SQL 待定 | 未配置返回零条，提交再次读取资格，禁止 fallback |
| 长链接归因 | 复用原合成任务关联，宏内容不可递归替换 |
| HTTP 重试/进程中断 | preparation operation_id+payload hash；版本 CAS；发布 lease generation 与 unknown fence |
| 外部提醒结果未知 | 通知 outbox 持久化，未知不自动重发；后台始终可审核 |
| 旧 worker 内存代码 | 上线必须刷新旧 worker，避免旧 claim 抢到新 workflow |

负责人：主实现负责 workflow/runtime 与部署；上传代理负责持久化与 Google 阶段；前端代理负责交互与服务回归；独立 HTTP 代理负责鉴权接入。产品确认已由用户给出，无新增产品决策阻塞开发。待用户补充的是业务素材筛选 SQL。
