# 测试与生产验收报告

验收时间：2026-10-19 16:33（北京时间）。

## 结果
- 冻结存量730张，全部正式入库并取得730个互不重复的正式素材ID；729张新上传，1张既有试传被认领。
- 分类：729张16:9横图、1张1:1方图。保留每张原始审核图字节与尺寸。
- 每项正式记录均验证产品Drama-AI素材、图片类型、剧目content_id、分类、名称和三个人员归属字段。未确认、失败、阻断及待上传数量均0。
- 全部出站attempt最多1；已存在试传attempt=0，不重复上传。素材接收行与正式行分开对账。
- 730个源文件SHA预检通过；5位创建人各1张代表图及1张方图，共6张正式素材图片下载SHA与审核图一致。
- 本地及Linux20项测试全部通过，覆盖快照范围、发布门槛、身份归属、未知结果与崩溃不重发、试传认领、字符集和安全预上传重试。Python编译与Git差异检查通过。
- 独立timer已enabled/active，完成后继续扫描新发布记录；发布worker与unified-writer PID保持。未调用YouTube平台写接口，没有人为创建测试视频。

## 发布与证据
运行代码5870e1126403263dcd1dbaf28e6cfe297dc43f29，已GitHub推送并由CPU精确fetch部署。
服务器持久证据：/mnt/data-disk/youtube-cover-material-sync/final-acceptance.json、material-ids.txt、backfill-manifest.json、sync.sqlite3。
冻结manifest SHA256：3786ddabc8319bfa2649aa2ab1c87948426478683a2af47f289b82b8e6e755cf。

本地交付：D:/reports/youtube-cover-material-sync-20261019/acceptance.json、material-ids.txt。
回滚边界及精确命令见deploy.md；维护skill已更新。历史WAL旁文件与字符集兼容问题均修复并完成服务器验证。

## 后续行为边界
仅同步真实发布成功且最终封面通过审核的任务，使用本次确认的ai-source参数。定时发布仍未公开或结果未知的视频不作为新发现任务；已明确授权的本次历史清单不受此增量门槛限制。回传不确定时保留账本对账，不盲目再发，也不影响视频发布。
