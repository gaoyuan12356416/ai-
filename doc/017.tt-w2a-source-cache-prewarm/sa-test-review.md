# SA 测试用例评审

## 结论

通过。测试设计已覆盖“HTTP 200 但实际 ID 不一致”、源故障不得负缓存、数据盘持久化、跨进程租约、3 日轮转预热、接口兼容和 featured LKG，并覆盖长驻 API 运行中数据盘掉载的 connect 前后设备身份复查。最新本地门禁为 Python `104 tests OK (skipped=1)`、Node `53 assertions OK`、`compileall`、Python 3.9 AST 和 diff-check 均 OK；唯一 skip 是 Windows 上的 POSIX mode preservation，生产 Linux 必须补跑。最终审计无剩余 P0/P1/P2；生产 systemd、数据盘、公网和真实浏览器仍待验收。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 处理 | 状态 |
| --- | --- | --- | --- | --- |
| TR-001 | TC-002 | 仅验证结果不能证明没有加载页面资源 | 增加请求记录并断言只有 HTML GET | 已补充 |
| TR-002 | TC-006/007/008 | 不存在、默认回退和结构异常不能混为 404 | 分别验证负缓存与 503 不负缓存 | 已补充 |
| TR-003 | TC-019/020/021 | 仅测线程 single-flight 不覆盖 API/timer 跨进程竞争 | 增加 SQLite 租约、等待和过期接管 | 已补充 |
| TR-004 | TC-023 | 路径位于 `/mnt/data-disk` 不等于数据盘真实挂载 | 增加挂载、设备、UUID、空间和软链接门禁 | 已补充 |
| TR-005 | TC-026/027/029 | “近 3 日投放中”需可审计且有规模保护 | 固定上海自然日、查询条件、二进制 ID 和 5001 门禁 | 已补充 |
| TR-006 | TC-024/025/033/034 | 新资源源可能破坏旧 API、参数透传或 featured LKG | 增加完整兼容与失败回归 | 已补充 |
| TR-007 | TC-038 | 只测功能不足以证明 GitHub-first 可回滚 | 增加 commit、release、manifest 和回滚演练 | 已补充 |
| TR-008 | TC-024 | 内部缓存状态可能扩大公开 API 枚举 | 断言 `ORIGIN_FILL/NEGATIVE_FILL -> MISS`、`DISK_HIT -> HIT` | 已补充 |
| TR-009 | TC-023/032 | 只验证目录存在不足以证明 API 与离线任务可共享 SQLite；全局 UMask 会误伤单体后台其他模块 | 增加“API drop-in 无全局 UMask”、state 固定 `install`/owner/group/`2770`、DB/`-wal`/`-shm` `0660` 检查 | 已补充 |
| TR-010 | TC-026/028/031 | 候选重排、候选变化与失败积压可能破坏花费优先级或轮转公平性 | 增加固定表/索引、保持花费排名、cursor v2 `next_content_id`/`next_index` 接续、有界重试和 500/3000 硬上限 | 已补充 |
| TR-011 | TC-023 | 启动时 mount/UUID 通过不能覆盖长驻 API 运行中掉载，单一 connect 前检查还有竞态窗口 | 增加首次记录 parent `st_dev`、每次 connect 前后复查软链接/父目录/设备号、变化关闭连接并 fail closed 的测试 | 已补充并通过 |

## QA 修订确认

38 个验收用例已按最新设计更新，TC-023 已纳入运行期掉载和 connect 前后身份复查。本地 Python 运行 104 项并整体 OK，其中 1 项因 Windows 平台限制跳过；Node 53 条断言全部 OK，`compileall`、Python 3.9 AST 与 diff-check 均 OK。该 skip 不得记为通过，必须在生产 Linux 验证 POSIX mode preservation；Linux 还需验证真实设备身份保护。数据盘/timer、真实浏览器、生产性能和 GitHub-first 回滚仍待执行；不得把本地 canary 记为生产证据。
