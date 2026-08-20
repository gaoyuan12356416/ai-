# SA 评审意见

## 结论

有条件通过。必须使用显式 deferred 标记，不能把所有空指纹队列自动视为可发布；必须保留实际发布探测、Premium/Relay、unknown 和 429 门禁，并同步修正短剧已知失败的全局阻断。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | queue 合同 | 直接删除预检会让历史空指纹/缺字段队列被误放行 | 新增显式 `media_validation_mode`，默认 `preflight`，仅 schedule-plan 可写 deferred | 已采纳 |
| SA-002 | P0 | publish | 实际 probe 决定上传类别和归因，不能删除 | 只取消建队前重复下载/probe；发布时保留一次 | 已采纳 |
| SA-003 | P0 | drama runner/store | 旧逻辑任一已知失败停止整批并把全池置 needs_review | known 失败继续后续并局部隔离；unknown 才全局暂停 | 已采纳 |
| SA-004 | P0 | relay | 短剧时长未知，非 Premium direct 可能在 actual 阶段失败 | 预先冻结同语言 Premium Relay；实际探测不做时长漂移比较 | 已采纳 |
| SA-005 | P1 | storage | 建队前按 N×512MiB 检查与实际串行单文件不符 | schedule plan 只要求一份最大媒体空间，point-of-use 继续复检 | 已采纳 |
| SA-006 | P1 | pool errors | 旧媒体预检错误会让池内条目在新逻辑下仍不可见 | 仅媒体下载/probe/repair类错误纳入受控可复检；合规/映射错误保持阻断 | 已采纳 |
| SA-007 | P1 | rollback | 回滚代码但保留 deferred 行会被旧版拒绝 | 回滚前先停止 timers；若已建 deferred 队列则继续用新发布层清账，不切回旧代码处理这些队列 | 已采纳 |

## 决策记录

- 用户明确覆盖旧的“建队前完整媒体指纹”定时排期合同；覆盖范围仅素材池和短剧池 schedule。
- 不把 GPU repair 内联到 actual publish，避免换一种形式继续头阻塞。
- 账号/Token 的轻量校验仍保留，且每条发布前再次实时验证。

## PM 修订确认

需求已补充显式模式、Relay、失败隔离、兼容性和回滚限制，可进入开发。
