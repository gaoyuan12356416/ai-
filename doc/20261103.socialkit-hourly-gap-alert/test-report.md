# 测试报告

## 七日规则变更验收（2026-11-03）

最终功能提交 `02c2b2dffc7a4c61e5019a3b740c19d2a412f213`。本地社媒相关测试 321 项，318 通过、3 项仅限 Linux 跳过；服务器规则与来源范围测试 50 项全部通过。Python/JS 语法、systemd unit 验证及 git diff 检查通过。

真实七日预览三平台收入源查询全部成功；规则与日期范围、逐日覆盖、SocialKit 精确 ID 区间汇总、发送账本去重已核对。少数既有归因不确定记录继续保持 pending，不当成零、不发送；预览 summary 因 pending 标记 partial，不是平台查询失败。真实金额、剧目、候选数量仅存私有预览目录。

早期全来源查询和仅按 Page 过滤查询触及超时；最终内部采集启用现有 `adset_dt` 索引，保留精确三元组及日期覆盖验证，三平台最终查询无错误。默认网页/日报路径不变。

北京时间 14:42 窄部署完成；timer active/enabled，下一次 15:00 启动。未手工发送新卡，也未清理或绕过原发送账本。主 API/Nginx/发布服务无需重启。回滚 manifest `/mnt/data-disk/socialkit-analytics/backups/hourly-20261103T064200Z/manifest.json` 已验证。以下为初版当天零收入口径的历史验收记录。

本地检查：SocialKit 完整测试集 115 项通过（包括 26 项新提醒测试及浏览器界面验收）；共享社媒测试 79 项通过；归因/范围/查询测试 89 项中 86 项通过、3 项因仅限 Linux 跳过；兼容日报测试 28 项通过。Python 编译、JS 语法、git diff --check 通过。

共 311 项被执行/发现，其中 308 项通过、3 项平台条件跳过。八个共享社媒模块及 YouTube 查询依赖与生产主 API 规范化源码 SHA-256 一致。兼容日报支持代码和测试从同一已验证来源版本纳入 release，但不替换线上原日报。

独立审查无剩余阻塞项，见 sa-code-review.md。

## 服务端与生产验收（2026-11-03，北京时间）

- Python 3.9：SocialKit 115 项，112 通过、3 项浏览器条件跳过；Facebook 46 项全部通过，覆盖本地跳过的 Linux 用例。本地浏览器已验证。
- 功能提交 c56cc94f5cad6c9d5af3a81247bb9438f0965b74 已经 GitHub 推送并由 CPU 拉取部署；源码/units 语法和干净状态核对通过。
- 真实三渠道预览 complete，源日期/来源更新时间对齐；首次发送获得回执，飞书 GET 读回确认目标群与 interactive 类型。真实金额、明细和回执仅存私有数据盘。
- 全量周期：11:37 启动，11:59:30 发布快照，12:01:51 完成提醒；15 个 UTC 日期全部核验，last_full_refresh_beijing_date 正确提交。已提醒事件未重复发送。
- 12:00 timer 正常触发，与尚在收尾的全量实例合并，未并发启动。12:03:05 手工补验日常增量入口，窗口只含 11-01 至 11-03；12:06:29 发布，12:09:13 完成，刷新和提醒均成功、零重复事件。
- timer active/enabled，下一触发为 13:00。公网 HTML/JS 200、小时文案及新缓存版本正确；匿名 API 401。共享主 API 未重启，其他发布服务/原日报未替换。
- 窄回滚包验证通过：/mnt/data-disk/socialkit-analytics/backups/hourly-20261103T033632Z/manifest.json。查询技能及后台维护上下文已同步。
- 增量收尾时旧中间版本清理达到既有 120 秒预算，报表/提醒成功不受影响；追加清理已成功完成（incomplete=false），当前/上一 manifest 均受保护，结果在 /mnt/data-disk/socialkit-gap-alert/acceptance-cleanup.json。

结论：核心功能与生产运行验收通过。不得将源库未入库数据解释为实时最终收入；当天仍会回补。
