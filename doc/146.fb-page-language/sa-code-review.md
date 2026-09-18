# SA 代码评审

## 结论

2026-09-18：Page 语言与未来运行扩充代码评审通过，独立组合测试 39/39 通过；原完整 FB 回归 186 项证据保留，最新 192 项通过。BUG-001/002 已关闭，应用 `90faff9` 已上线；732 条追加在维护窗口内完成，原历史三表逐行不变。

## 评审范围与证据

- `languages.py` 使用既有别名/格式规则，每种有效 Page 语言获取一次候选；再检查候选语言。
- `core.py` 仅从对应语言快照选择；空语言不选材，二次读取出现新语言时零落账退出。
- `validation.py` 兼容旧输入，规范化返回对象省略语言。
- 模板 HTML/JS 差异已审阅：语言控件、加载、提交全部删除，JS 资源版本已更新；主任务正式 URL 的未登录 DOM 读回确认语言控件为 0，帮助文字和脚本版本正确；登录后保存交互未执行。
- 既有运行的重复时隙直接返回，后续时隙创建不会修改旧任务或 Page 快照。
- `repositories.py` 的候选上限保持精确前 N 排序；N 大于占用加 50 时，排除占用后前 50 集合与原选择语义一致。`core.py` 和扩充事务再次核对占用数，防止并发增长破坏这个边界。公开校验仍不接收该内部字段。
- `pool_extension.py` 已验证精确未来运行、语言隔离、旧字段不变与跨运行回滚；完整匹配的回执在素材查询前返回，事务内仍复查以防并发重复。BUG-001 复现测试已转为通过。

## 待完成项

主任务已完成 BUG-002 部署、TL 500、7 种语言候选审核冻结及 732 条追加，原三表逐行不变，五 timer 恢复并自然制作。服务器巡检已部署且首次报告正常，见 production-verification.md；实际发布须到期后由平台确认，登录后保存未操作。生产 HTML 保留较新的 `quick-nav.js?v=20260915retired` 版本，不恢复已下线模块。

## BUG-002 后续审阅

已只读检查 `_https` 的窄范围兼容及新增四项 URL 测试：仅已验证自有 COS hostname 的无凭证、无端口、无 fragment HTTP 输入可更换为 HTTPS，完整路径与 query 保持，其他 HTTP 不放行。主任务提供的五个相同对象 HTTPS HEAD 均为 200/video/mp4，且 TL 描述和剧上线条件满足，因此原 TL 候选 0 来自 URL 过滤，不能归因于素材或描述缺失。

此补充修复 `f7cb58f` 已部署，TL 重建 500。两项旧预期更新后最新 192 项全套通过（23.191 秒），应用最终 `90faff9` 已上线；不覆盖既有 39/186 项历史证据。

## 操作 CLI 只读审查

已读取 `scripts/fb_extend_future_pool.py`，未修改或运行该脚本。

| 编号 | 级别 | 具体边界 | 必需控制与状态 |
| --- | --- | --- | --- |
| CLI-01 | P1 | live 新任务绑定、旧任务和发布事实的比较发生在 `extend_future_runs` 提交之后；并发写入可能造成事后才发现差异 | 只能在维护窗口执行。主任务确认五类 timer 已停、preparing/running/submitted/unknown 为 0，无其他自动模板；从预演到 apply 后读回持续暂停 |
| CLI-02 | P1 | 候选缓存被预演与真实写入分别读取，脚本仅查 mtime 与配置 metadata，没有对文件内容做内置 SHA 绑定 | 主任务已在唯一审计进程完成后冻结 JSON 与 metadata、chmod 0440、记录 SHA，再进行预演与 apply |

上述依赖已明确写入部署文档，不能将该 CLI 当作无需暂停的在线扩充工具。维护窗口外的通用并发安全未由本次测试证明。

## 实际验证结果

`python -m unittest scripts.test_fb_page_languages scripts.test_fb_pool_extension -v`：39 项通过，0 失败，5.694 秒。测试仅使用本地临时 SQLite 与模拟候选仓库。

## 服务器只读巡检审阅

`fb_page_readiness_watch.py` 只读 SQLite 并输出数据盘报告，不发布或重试。已发现并修复 `failed_without_retry` 漏报；scope 增加 145 行/145 唯一 Page/非空语言约束。ETA 累计所有较早时隙待制作工作，按输入时长缩放；submitted 与 published 保持区分。

新增 `scripts/test_fb_readiness_watch.py` 17/17 通过，0.014 秒，包含较早时隙长视频压缩后续时隙余量，以及超过 2 小时仍未确认的边界。三条 timer 日期显式 Asia/Shanghai，主任务已用 CPU systemd 239 验证；unit 与脚本分别使用 RequiresMountsFor/ismount，避免未挂载时写根盘。Codex heartbeat 已 PAUSED 并读回；主任务已部署巡检 unit，首次报告正常，精确证据见 production-verification.md。
