# Design review
Basis: independent COS/library and render/runtime source reviews plus confirmed operator choices.
| Issue | Priority | Required resolution |
|---|---|---|
| Old canonical payload drops trims | P1 | New type, versioned recipe/fingerprint |
| Separate GPU process bypasses concurrency | P1 | One runtime, dispatcher and slot |
| Old binary cannot read new records | P1 | Mixed reader retained; rollback disables intake |
| Newest CPU branch lacks GPU enhancements | P1 | Pin live GPU baseline; scoped overlay |
| Existing uploader permits local fallback | P1 | Dedicated COS-only private verified upload |
| Network ambiguity duplicates writes | P1 | Durable operation IDs and reconciliation |
| Shared library expands project write scope | P1 | Independent object ownership |
| Crossfade changes timing | P2 | Explicit overlap/frame rounding/short-clip checks |

All resolutions are acceptance requirements. This design review does not imply production validation.


## 2026-10-22 BUG-008 设计复核

独立前端审查确认：addEpisodes在prepare后直接return，6秒轮询只刷新metadata；等待后未检查项目归属；轮询前20个unprobed会饿死较后选中集。采用绑定项目/弹窗/目录代次的pendingAdd，确认全部ready后恰好一次添加，关闭或切换取消自动继续。

独立后台只读审查确认第三集本次校验总23秒、generation=1成功，不应重建或取消任务。四轮80次来源查询中76次查询未选无任务剧集。产品读取使用共享600秒本地缓存，并非重复SQL；本轮保持静态文件范围，无需新增后端缓存或重启GPU。

独立异步边界复核要求：未保存项目使用稳定客户端对象/代次，不能以空server ID识别；正常autosave赋ID不取消等待，重新载入同ID项目必须取消。先消费pending token，再重验容量并添加一次。旧操作finally不得恢复新操作的按钮或提示。probe没有客户端operation_id：响应未知时仅GET来源核实，取消等待仍保留未知记录；任务404也可能为权限隐匿，不能据此重建probe。轮询不得重复发恢复POST。
