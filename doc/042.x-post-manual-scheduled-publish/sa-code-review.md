# SA 代码评审

## 结论

通过。P0/P1 设计项均已实现；评审及 live-baseline 重放发现的占用/素材复用兼容问题已修复并回归，无未关闭阻断项。

## 评审范围

- SQLite 增量 schema、trigger、reservation 状态机和到期 claim。
- 旧立即请求、auto_template 与历史任务兼容。
- `09d267db…` operator-manual pool/历史素材复用兼容，且 future reservation 不进入自动候选。
- 主 API/Sidecar 的字段透传、安全 DTO 和审计。
- 浏览器北京时间语义、幂等指纹、等待状态和轮询频率。
- 双 runtime、Nginx static 与 systemd 部署/回滚边界。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | `features/x_posts/service.py` timing trigger | 只靠应用标准化时，非规范时间若绕过应用写入会破坏字符串到期比较 | DB trigger 强制 `YYYY-MM-DDTHH:MM:00Z` 且 `strftime` 可解析 | 已修复 |
| CR-002 | P1 | `query_material_keys()` | active reservation 未进入自动模板选材前的占用查询 | 合并 active reservation，并验证 released 后恢复 | 已修复，见 BUG-001 |
| CR-003 | P1 | 部署顺序 | 新前端若早于新 Sidecar 暴露，scheduled 字段可能被旧边界忽略 | 停 timer；先 Sidecar/schema，再 main API，最后 public static | 已写入部署门禁 |
| CR-004 | P2 | UI 轮询 | future run 若一直 2.5 秒轮询会增加负载 | 大于 60 秒时最多每 30 秒，临近后 2.5 秒 | 已实现 |
| CR-005 | P0 | reservation insert guard | 重放到 `09d267db…` 后，初版 trigger 会重新拒绝 operator-manual 选择既有 pool/历史 queue，回退刚上线的复用能力 | 仅 auto_template 对 pool/history fail closed；manual parent 只拒绝其他 active reservation | 已修复 |
| CR-006 | P1 | `available_pool_items()` / pool DTO | future manual 可在既有 pool 上建 reservation 后，自动排期仍可能把该 pool 行当作 available | 自动可用查询排除 active reservation，pool DTO/summary 派生为 occupied，并保留原 pool 行 | 已修复，见 BUG-002 |

## 编译 / 验证结果

- `python -m py_compile ...`：通过。
- `git diff --check`：通过。
- 40 个 `scripts/test_x*.py` 模块逐个隔离执行：627 项，625 通过、0 失败、2 跳过。单进程 discover 在 Windows 偶发 `10053` 连接中止；对应 `test_x_accounts` 60 项及受影响用例隔离复跑全部通过。
- Playwright 本地 mock smoke：立即/定时切换、北京时间、按钮状态、202 DTO 和等待定时状态通过；截获 payload 含 `scheduled_at=2026-08-13T15:00:00+08:00`，未访问 X。
