# Acceptance cases
| Area | Scenarios |
|---|---|
| Auth | Missing Cookie/API token/module rejected; cross-owner writes denied; admins inspect |
| Navigation | Stale config gains group once; disabled choice preserved; user permission assignable |
| Upload | Multi-file/cancel/retry one part/expiry/wrong size or digest/forged ID/missing parts/repeat completion |
| Ambiguity | Init/complete/submit disconnect reconciles durable identity; no false ready/duplicates |
| Privacy | Signed reads work; anonymous denied; CPU JSON only; no local fallback |
| Library | Probe pending/failure, search/rename/soft-delete, frozen references survive |
| Timeline | Delete two intervals, reorder/undo/repeated asset/remove episode/per-drama isolation/autosave conflict |
| Shared settings | Insert/fade application preserves episode trims |
| Render | Two dramas two outputs, contain/cover/cut/fade/short-clip error/mute/no-audio/mixed fps and size |
| Output | Download real MP4, verify profile/duration, inspect boundaries and A/V alignment |
| Recovery | Same operation ID, retry only failed child, CPU loss/GPU restart/stale generation/unknown process |
| Compatibility | Old hash/DTO/cache, shared slot, old prefetch skips new type, mixed-reader rollback |
| Cleanup | COS verified before removal, stopped failed task TTL, receipts retained |
| External | No processing/ingestion request; pending integration shown |

Mocks, real network checks and actual-media validation are reported separately.


## 2026-10-22 BUG-008 回归范围

- 延迟产品、素材、搜索、剧集和项目读取：动作当次即显示加载，重复点击不重复请求，失败后恢复并在当前面板显示原因。
- 选择3集，2集ready、1集延迟probe：单次点击显示2/3，期间保持选集且禁止重复添加；最后一集ready后自动添加3集、关闭弹窗，草稿只添加一次。
- 选中超过前20个位置的集：只跟踪本次选集；慢轮询不重叠，不请求未选unprobed；单项读取异常不吞掉进度/错误。
- 关闭按钮、ESC、取消等待、产品/短剧切换、新建/切换项目：旧probe和目录响应返回后不修改新项目，不重开弹窗、不误保存。
- 搜索A慢B快、选剧A慢B快、清空产品：旧响应不能恢复旧目录。
- 原任务failed/unknown/recovery_required：核实同一任务，禁止替代任务；共享active任务无私有task读取权仍可跟踪来源。
- 已成功保存但项目列表慢：不阻塞后续项目动作；重复制作沿用原operation ID，成功任务不重做。
- 正式站保持标准蓝色、共享导航/用户栏、已存项目版本及成功成片；临时测试只用本地模拟后端，不提交新的真实制作。
