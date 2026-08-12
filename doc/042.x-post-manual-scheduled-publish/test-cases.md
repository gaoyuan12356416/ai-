# 测试用例

## 测试范围

SQLite 增量迁移、时间/幂等校验、素材占用、到期 claim、Sidecar DTO、主 API 路由、弹窗交互、runner 无提前执行与原有立即/自动模板回归。

## 测试数据

- 临时 SQLite 与 mock Sidecar/MySQL/media；禁止真实 X 网络写入。
- 固定当前时间 `2026-08-12T06:00:30Z`（北京时间 14:00:30）。
- future slot `2026-08-12T14:10:00+08:00`，past slot `2026-08-12T13:59:00+08:00`。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 旧请求立即发布 | 旧三字段 payload | 创建 run | mode=immediate、scheduled_at 为空、可立即 claim | P0 | 通过 |
| TC-002 | 新立即发布 | mode=immediate | 创建并重复提交 | 同一 run，原行为不变 | P0 | 通过 |
| TC-003 | 创建未来定时任务 | future slot | 创建 run | HTTP 202、UTC 时间/北京时间日期冻结、active reservation | P0 | 通过 |
| TC-004 | 到期前 claim | scheduled run 未到期 | 多次 claim/tick | found=false/no_pending，零 queue/log/X call | P0 | 通过 |
| TC-005 | 到期 claim | 时间推进到 slot | claim 两次 | 第一次取得同一 run 并置 running；无重复 run | P0 | 通过 |
| TC-006 | 时间格式边界 | 无时区、秒非零、过去时间 | 创建 run | 400 invalid_request，零写入 | P0 | 通过 |
| TC-007 | 过期幂等重放 | 原 future run 已创建且时间已过 | 同参同 key 重试 | 返回原 run，不因 past 校验失败 | P0 | 通过 |
| TC-008 | 时间幂等冲突 | 同 key 改时间/方式 | 重试 | 409 idempotency conflict | P0 | 通过 |
| TC-009 | 手动 reservation 冲突 | future run active | 再建手动/自动模板或新加 pool | 全部被拒绝；提交前已有 pool/history 不阻止本次 manual | P0 | 通过 |
| TC-010 | queue reservation guard | future run active | 其他 parent 插入同 material queue | DB trigger 拒绝 | P0 | 通过 |
| TC-011 | 所属 run 建队列 | future run 到期且预检通过 | create plan | 原子 queue 创建，reservation consumed | P0 | 通过 |
| TC-012 | 预检失败释放 | 到期后无 queue | record failure | run failed_preflight，reservation released，可再提交 | P0 | 通过 |
| TC-013 | unknown/failed 保护 | 已生成 queue | 模拟失败/unknown | 该 queue 不自动重试/改写；自动流程排除素材，operator manual 复用只创建独立新 queue | P0 | 通过 |
| TC-014 | schema 幂等 | 旧库 | ensure_storage 两次 | 新列/表/索引/trigger 单份，旧行默认 immediate，integrity ok | P0 | 通过 |
| TC-015 | 自动模板兼容 | auto_template task | 创建/claim/recover | 固定 immediate，原测试全通过 | P0 | 通过 |
| TC-016 | API 兼容字段 | 旧/新 payload | app route | 只接受允许字段；透传并审计 mode/time | P1 | 通过 |
| TC-017 | 安全 DTO | store 含 reservation/内部字段 | public response | 只返回 mode/time 白名单，不泄露内部表/凭据 | P0 | 通过 |
| TC-018 | UI 默认立即 | 打开弹窗 | 检查 DOM/state | 立即选中、时间隐藏、按钮立即文案 | P1 | 通过 |
| TC-019 | UI 定时校验 | 切定时 | 空/过去/未来输入 | 空/过去阻止提交；未来 payload 含 +08:00 | P0 | 通过 |
| TC-020 | UI 状态/轮询 | future DTO | render/poll | 显示北京时间与等待定时；远期 30s、临近 2.5s | P1 | 通过 |
| TC-021 | 原有立即 runner | immediate run | mock 全批预检与 publish | 行为/停止语义不变，无真实 X call | P0 | 通过 |
| TC-022 | 定时任务保留手动复用 | 素材已在 pool，随后已有 manual queue history | 分别创建 future run、到期建队列、再创建 future run | 两次 scheduled 创建均允许；active 时自动 available 列表排除且 pool 显示 occupied；旧记录不改写 | P0 | 通过 |

## 回归范围

- `test_x_post_priority_manual_store.py`
- `test_x_post_manual_sidecar.py`
- `test_x_post_manual_runner.py`
- `test_x_post_multi_schedule_ui.py`
- `test_x_posts_app_contract.py`
- `test_x_post_auto_template_bridge.py`
- 现有 X posts / schedule / ledger 聚焦套件。
