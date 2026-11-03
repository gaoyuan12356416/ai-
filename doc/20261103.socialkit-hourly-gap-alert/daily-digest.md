# 每日 11:30 汇总与 HTML 明细

原提醒将大量命中项按十条拆卡，导致群消息过多。用户要求先停，再汇总附 HTML，并明确每天北京时间 11:30 只播报一次，今天手动补发。

小时刷新继续运行，但 `hourly.py` 不再调用发送程序。独立 `socialkit-gap-digest.timer` 在 Asia/Shanghai 11:30 启动 `features.socialkit_gap_alert.digest --send`，读取已发布快照并采集比较数据。实际发送发生在有界采集完成后。无错过补跑（Persistent=false）；不设自动重试调度。

一条卡片包含总项目数、不同剧目数、平台汇总、重叠原因数、待核实数和 HTML 按钮。HTML 使用现有 CPU COS 发布器，包含所有命中项目、待核实项目、来源时间和口径，支持平台/原因筛选、剧名或 ID 搜索及收入排序。所有业务字符串 HTML 转义，无外部脚本。收入规则沿用七个完整 UTC 日口径；汇总仅包含命中项目，不等于全部平台收入。

独立 `digest-delivery.sqlite3` 按北京时间日期+群去重。手工补发也占用当天唯一回执。HTML 发布成功后冻结卡片、URL及摘要，再认领发送；已发送/发送中/未知结果不自动重发。明确失败仅允许保留相同卡片、相同幂等 UUID 的有限重试，未知结果必须先核对。旧 `delivery.sqlite3` 及分卡回执保留，不删除、不复用旧事件。预览不发布 HTML、不认领、不发群。

手动补发可指定最近两小时内的已采集 `comparison.json`，避免再次查询。仅允许当前 UTC 统计日。正常定时采集复用现有新鲜度及完整性检查；缺失归因单列，不当作零。

安装使用 `ops/socialkit-analytics/install_hourly.py --install COMMIT`，仅切独立 release、报表静态资源及本功能四个 units。验证后恢复小时 timer，并 enable/start daily timer。主 API、社媒发布器无需重启。回滚先 verify-rollback 再 rollback 新备份 manifest；安装器停两个 timer，获取刷新与提醒锁，恢复旧指针/文件并关闭新 daily timer；保留所有快照和发送账本。若原小时 timer 因本次紧急暂停被记录为 inactive，回滚后由操作员显式恢复。回滚旧版本将恢复原小时分卡行为，故当前业务需求下应优先保持提醒暂停。

验证：大规模单卡和完整 HTML、注入转义、预览无副作用、当天手发去重、隔天发送、未知回执不重试、明确失败冻结重试、上传失败不认领、跨日中止、小时刷新不发送、定时器无补跑；旧七日规则和归因测试继续通过。真实收入和群回执仅保存私有数据盘，禁止入公开 Git。

部署完成：运行提交 `c409299d7177e67597f7384a0579bb2ee5f90370`，CPU 精确 release 指针已核验；今天手动汇总已发送并读取飞书消息确认，下一次每日 timer 为北京时间 2026-11-04 11:30。小时刷新 timer 已恢复。最终备份 `hourly-20261103T103131Z/manifest.json` 位于 `/mnt/data-disk/socialkit-analytics/backups/`；完整回退先使用该 manifest，再使用首次功能切换备份 `hourly-20261103T103001Z/manifest.json`，每步先 verify-rollback。保留旧账本和日报新账本。
