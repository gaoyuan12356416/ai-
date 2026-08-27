# GG 图片/视频基准与1000美元门槛发布

状态：本地实现完成，待最终回归、GitHub推送与生产验收；不是已上线声明。

## 已批准范围

2026-01—2026-07仅GG刷新；A分母/CPC、B的CTR均使用全部图片视频资产（含未映射），按NG/PK分别加权汇总。A不设最低消耗；B严格>1000美元。7月批准样本：NG40、PK6；A-only8、B-only28、A+B10；USD116640.81。

## 部署及回滚设计

- 旧current：`/opt/opay-excellent-creatives/releases/72f2e7440e16d8d3ea782ce9eea31176d21c0797`。
- 旧data_version：`20260827T152235588279+0800`；latest SHA256：`465e4e10c9c1cf9c38ecf24a246a47bdf84fe2d782022fc03b98e11ef6693b13`。
- 数据盘UUID已核对：`3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`；2026-08-27预检可用77G，根7.1G。所有持久报表数据放数据盘。
- 新缓存：`/mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives-google-picvid-1000.sqlite3`。
- 备份目标：`/mnt/data-disk/opay-excellent-creatives/backups/20260827-pre-google-picvid-1000`。
- 影子目录：`/mnt/data-disk/opay-excellent-creatives/staging-public-google-picvid-1000`。
- 验收目录：`/mnt/data-disk/opay-excellent-creatives/qa/acceptance-20260827-google-picvid-1000`。
- 先GitHub精确commit，再在服务器独立release运行；`--clone-cache-from`克隆旧库，`--backfill --from-month 2026-01 --to-month 2026-07 --google-only --refresh --rebuild`重算。正式切换前必须运行独立raw-cache校验器，包含`--approved-july fixtures/2026-07-google-picvid-approved.json`。
- 切换前保存HTML/latest、配置哈希及旧库一致性备份；发布持有独立锁，确认refresh服务无在途，暂停本报表timer；发布失败时恢复旧HTML/latest/current后恢复原timer状态。不修改Nginx/env/unit或其他报表，不重启主服务。
- 手工回滚：在独立锁内确认当前为本release与记录的数据版本；暂停两个timer，恢复备份HTML/latest，原子切current至旧72f2e7，验证旧manifest哈希后恢复timer。旧release默认指向旧google-cpc缓存，保留新旧数据，不覆盖旧缓存。

## 实际执行记录

待补充：精确提交、测试统计、7个月重算结果、审批样本比对、公开HTTP/浏览器检查、失败保留与回滚验证。
