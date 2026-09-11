# SA 代码评审

## 结论

2026-09-11：独立评审通过，未发现未解决的发布阻断问题。确认检查冻结后的 engine/core/service/HTTP/通知器、定时 UI 与部署脚本；结论基于本地代码、隔离状态机测试及浏览器 mock，不能替代真实平台预约回执。

## 评审范围

- private 上传 → 已审核封面 → processing succeeded → 原生 publishAt → actual public → 可选首评。
- 发布安排版本 CAS、准备入队与 worker 租约、持久写入意图、已上传原视频 ID、未知结果只读核验。
- missed/armed/control_pending/reconciling/cancelled 的 API 投影、固定北京时间转换、实际读回前禁止成功提示。
- 共享账本旧行增量迁移、旧 lane 隔离、通知 outbox 历史事件键与预约事件稳定性。
- scripts/deploy_youtube_schedule.py 的基线 SHA、GitHub 精确提交、局部 app handler 替换、数据盘备份和保留数据库回滚。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 | P1 | service._ledger 的写事务调用 | SQLite 写锁与 store._lock 顺序相反。 | 在同一事务连接读账本。 | BUG-001 已修复并测试 |
| CR-02 | P2 | service.review/_mark_schedule_missed | 审核过期任务清空 missed_at，引入重复通知。 | 保留同一预约初次错过时间。 | BUG-002 已复现、修复并测试 |
| CR-03 | P2 | engine/core/failure_notifications | 预约未知轮询更新 updated_at，使一次未知产生多个通知。 | 持久 schedule_event_at；只读轮询保持同一事件键。 | BUG-003 已修复，状态机与 outbox 双层测试通过 |
| CR-04 | P2 | service.schedule | 准备操作跨入队后相同 op 的超时重提会再路由到新账本。 | 先识别已接受的准备操作，再返回当前 DTO。 | 已修复并测试 |
| CR-05 | P2 | service._schedule_dto / HTTP tests | 对失败/临近预约显示不可执行控件，HTTP path 枚举遗漏新路由。 | DTO 与后端拒绝条件一致；增加 schedule 到通用权限/Origin/JSON 回归。 | 已修复并测试 |
| CR-06 | P2 | 历史剧集关联测试 fixture | Mock app 没有真实路径 JOB_DB_PATH，运行时 Path(Mock) 失败。 | fixture 显式使用临时数据库路径。 | BUG-004 修复，仅测试修改 |

## 编译 / 验证结果

- 最终冻结代码：17 个 Python 模块合计 507 次用例执行，506 通过、1 Windows 符号链接能力跳过；另 2 个工作流+真实账本+假平台集成用例通过，总计 509 次执行、508 通过、1 跳过。
- 新增专项：引擎 33、准备/通知 15、集成 2，共 50 项通过。
- 浏览器：定时专项 38、防闪屏 29，共 67 项通过，来自前端独立执行及保存的结果 JSON。
- Python 编译、JS 语法检查通过；完整命令、历史 broad browser fixture 限制见 test-report.md。
- 部署脚本静态评审通过：部署与回滚前后复查活动/未解决预约；恢复只覆盖受管代码；主数据库、通知 outbox 与图片资产保留；异常路径再次启动服务。真实部署检查由主任务执行并另记。
