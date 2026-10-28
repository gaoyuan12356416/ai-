# 测试报告

2026-10-28，本地 Windows/Python 离线验证。Meta 全部为模拟传输，无真实广告写入。

| 检查 | 实际结果 |
| --- | --- |
| compileall 新模块、cover.py、共享 store.py | 通过，退出码 0 |
| 完整 test_cover*.py 快照 | 66/66 通过，7.850 秒 |
| 后续账本增量 test_cover_store.py | 31/31 通过，8.564 秒 |
| 旧共享/延期/账户级 Store 回归 | 55/55 通过，2.983 秒 |
| 完整清理模块回归快照 | 执行 641 项，1 项按环境跳过，其余通过，36.765 秒 |
| 最终 test_cover*.py 集成 | 98/98 通过，14.195 秒 |
| 线上只读兼容反馈后的集成 | 105/105 通过，13.739 秒 |
| Meta validate_only 参数投影后的集成 | 109/109 通过，12.932 秒 |
| 最终 compileall、安装器编译、git diff --check | 通过 |
| 生产吞吐/URL/绑定/DELETE/Feishu | 待 GitHub-first 首批验证 |

命令见 dev-plan.md。66 个快照包括 Graph 13、payload 10、source 5、Store 24、worker 14；后续 Store 增至 31，新增双库意图读取、256/257 通知、纯转批静默、资源只读恢复、未发送创建的中断恢复和已知封面索引防止下一小时重复转换。最终数量可能随集成继续增加。

已验证阈值、证据保存、32 线程去重、跨窗口等待、参数保留、直接 URL、共享/归档引用、分页失败、单次删除、失败继续、未知核实、原子清理入队、失败扫描恢复和晚到通知。

评审发现的事务窗口已在发布前修复。未将空白 BUG 模板当成实际缺陷；未声称生产操作完成。建议进入已授权的 GitHub-first 自然命中首批验收，只有真实参数回读、删除回执和限流核实完成才认定上线验收完成。

2026-10-28 回读修复：真实创建已被接受；4 个已知 ID 因 Meta 自动追加 Creative 管理名称后缀、GET 省略退役 standard_enhancements 聚合而隔离。仅允许原名称完整前缀加精确日期/32位十六进制后缀；仅在合法创建投影已省略聚合且逐项配置齐全时允许 GET 缺失聚合。广告标题、文案、完整链接、身份及逐项增强仍严格核验。110 项封面回归通过。旧失败任务不重置，未知对象只读核对后仅恢复首次未发送绑定。

绑定阶段真实样本确认：Meta 会将 post_engagement/post_interaction_gross/link_click 的旧帖子 ID 替换为新 Creative 的 effective_object_story_id，并重排 tracking_specs。仅当新旧有效帖子 ID 归属同 Page 且完整 tracking 多重集合（含重复数、Pixel、转化 ID、额外字段）一致时接受。绑定未知只读重新取 Ad/Creative 并验全参后确认，不重发 POST。111 项封面回归通过。

用户于本会话明确确认“允许该视频专用项不再回传，继续修复和替换”。仅允许已核实图片 Creative 的 video_auto_crop 不回传（原值必须为严格 OPT_IN/OPT_OUT 结构），发送仍保留原值，返回不同值仍拒绝，其他逐项功能缺失仍拒绝。封面回归 112 项通过。

真实样本发现 effective_object_story_id 可在绑定后才生成，故绑定后必须重新 GET 新 Creative 并验全参，再核对新旧帖子引用；仍未就绪时仅保留未知，禁止重发绑定。新增真实 SQLite worker 行为测试覆盖绑定后才有帖子 ID、延迟就绪后 GET-only 确认且仅一次 POST。封面回归 113 项通过。
