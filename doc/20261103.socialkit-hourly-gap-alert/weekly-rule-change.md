# 七个完整 UTC 日提醒规则变更

## 用户确认

只播报同剧同平台我方全部历史未查到成功发布，或我方区间收入明显低于 SocialKit 的剧。收入时间窗为最近 7 个完整 UTC 日；收入差距门槛为 SocialKit 至少 $10 且我方不足其 50%。未发布条件独立，只要求 SocialKit 区间收入 > 0。

## 实现与验收

修改独立提醒 collector、cards、runner，并为七日采集设置有界总预算；小时报表刷新、收入归因、发布服务及原日报口径不变。SocialKit 七日按完整快照汇总，我方逐日校验后累计；历史发布沿用全部可信成功记录。保留群+播报日+产品+平台+剧目去重，包括旧口径已发送/未知事件。

真实预览发现旧 Facebook 站点全来源逐日扫描会超时。因此共享 routes 新增仅内部采集可选的 `include_unconfirmed_sources`：按已有冻结 Page/素材来源候选收窄查询，包含未确认发布来源；精确来源三元组过滤和日期覆盖哨兵沿用既有实现。未确认发布但已有非零收入仍进入待核实，不误当零；无候选也先确认该站点该日期实际存在。默认网页/日报路径不变；本次只部署到独立提醒 release，不覆盖主 API 的 routes。

自动化验收覆盖 $9.99/$10 边界、49.9%/50% 边界、非零我方收入、未发布独立条件、双原因单事件、完整 UTC 日及跨年区间、7 日金额累计、缺一天不当零、历史源更新时间不误判陈旧、历史采集过期、全部发布历史、已有回执去重、发送未知回执与分卡。

## 发布步骤

分支 `codex/socialkit-weekly-gap-rules-20261103`。本地测试后先推送 GitHub，服务器 fetch 精确提交到 `/mnt/data-disk/socialkit-analytics/releases/COMMIT`。复用已发布 current/previous 快照预览 7 日规则；预览不发送且不写投递账本。记录候选数量、平台完整性、耗时和未达阈值的排除验证，真实剧目与收入明细不入 Git。

确认现有部署安装器涉及的静态文件及 unit 无漂移后，运行 `python3 ops/socialkit-analytics/install_hourly.py --install COMMIT`，记录新备份 manifest。原小时 timer 在安装期间短暂停止，运行中刷新不强杀；安装后恢复。主 API、Nginx 和发布服务无需重启。上线后以新代码进行预览/正常小时周期核验。

回滚使用新 release 的 `python3 ops/socialkit-analytics/install_hourly.py --verify-rollback MANIFEST` 核对，再执行 `--rollback MANIFEST`。只还原版本指针及安装器备份文件，保留快照、收入事实和投递账本，禁止重发未知结果。
