# 测试报告

## 测试结论

本地、Linux 与生产验收通过。2026-11-04 12:14 北京时间，精确提交 `ba6e31d31ffb0f9f24df82fbdf9213bed4445ce0` 已部署。

## 测试范围

Page/素材锁、历史 schema 迁移、同族/跨族串行、晚回执、过期租约、known-ID GET、Excel/素材池入口，以及统一调度既有 X/TT/YouTube 行为。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 原生隔离与入口反例 | 23 | 23 | 0 | 0 |
| 统一层定向与既有回归 | 105 | 105 | 0 | 0 |
| Linux 原生隔离 | 23 | 23 | 0 | 0 |
| Linux 调度 / 回执 / 原生导入 | 124 | 124 | 0 | 0 |
| Linux YouTube cover resource | 6 | 6 | 0 | 0 |
| Linux 未来只读排期切换 | 3 | 3 | 0 | 0 |

## 缺陷情况

修复三个问题记录 BUG-001/002/003；QA 反例与修复闭环见 sa-code-review.md。

## 验证证据

- `python -m unittest tests.test_fb_page_task_isolation`
- dispatch suite 包含 `scripts.test_pipeline_fb_task_isolation`、source_locks、adapters、core、coordinator、legacy_convergence、native_imports、youtube_resources。
- 所改 Python 编译与 `git diff --check` 通过。
- 两条既有目标任务的平台列表逐条只有 1 个匹配视频；原始 task、短链、快照保留，native 与 dispatch 均 published。运营明细保留在私有维护目录。
- Linux 业务回归使用 `937100c`；最后发布 `ba6e31d` 的 features 差异为空，仅完善部署操作与 3 项部署测试。初次命令误引用不存在的 youtube_resources 模块，修正为 youtube_cover_resources 后 6 项通过；其他 124 项先前已全部通过。
- 在线备份副本构造器重复迁移及 quick_check 通过；真实 fb-auto-post 账号可加载候选模块。
- 生产 11 个 FB 文件、14 个 coordinator 文件 SHA 全匹配；两服务 health=200，两个 live 开关保持 1。
- 18 条既有 unknown 的 Page fence 均已消失，各自 material fence 仍存在。截图 15 个素材 guards 均为空；原 batch 21 个接受任务、15 个 skipped 的 request_json 完全未变。
- 9 个 timer 状态、渠道配置恢复原值；临时 iptables 规则已删除，主 API PID 不变。专用审计目录 `/mnt/data-disk/pipeline-dispatch-runtime/fb-isolation-ba6e31d31ffb` 内有 manifest、apply、acceptance、runtime-restoration 与原库备份。
- 首次切换被已取消任务残留的 preparing 标签阻挡，未改运行版本；修正后严格请求排空遇长耗时未来素材查询。最终显式识别唯一未来 planner 后停止其纯读取，按原 due id/owner/lease 延后重试；原定次日 14:47 北京时间保持不变，无任务或平台 POST 重建。

## 遗留风险

身份缺失的历史未知记录无法安全缩小防重范围，仍需核对。明确失败不会自动新建重试；后续不同素材继续。原 Excel 跳过行的配置未持久化，由用户重传完整文件。

## 发布建议

已部署并验收。后续用户重新上传原 Excel；不自动补发 15 行。回滚必须保留已发生发布事实与 running-only schema 兼容代码。
