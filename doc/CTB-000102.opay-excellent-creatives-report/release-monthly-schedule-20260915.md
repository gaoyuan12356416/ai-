# OPay 报表月度更新时间调整

## 上线结果

- 已于北京时间 `2026-09-15 10:20:32` 生效；运行提交为 `207f42e1a6af2e53a1a507ba8b0b6f2d84c96ff6`，已推送 GitHub 并由服务器 fetch 检出。
- 主机 `43.166.187.96`，`/opt/opay-excellent-creatives/current` 已指向该提交的 release。
- 终版 timer 为 enabled/active，下一次 `2026-10-02 08:00:00 CST`，处理 `2026-09` 数据；初版 timer 为 disabled/inactive。
- 本地与服务器部署契约检查均 3/3 通过，`systemd-analyze calendar`、`systemd-analyze verify` 通过。已执行 `systemctl daemon-reload` 并启动新计划；公开页面和 latest JSON 均 HTTP 200，SHA-256 与上线前一致。
- 备份已校验，目录为 `/mnt/data-disk/opay-excellent-creatives/backups/20260915-monthly-schedule-0800`。完整证据为该目录的 `preflight.json`、`SHA256SUMS` 和 `deployment-result.json`；旧运行提交为 `295b14d162b85a397e02dfcdbd637ef4a497e4b5`。
- 停用初版 timer 时，systemd 可卸载已结束的 service 并清空内存中的最近运行时间。首次检查因此中止，随后通过 service journal、持久 timer 时间戳、公开数据哈希确认没有触发刷新，再完成切换。无业务服务重启，无历史数据重算。
- 通用技能上下文未改动；本报表的新调度已记录到项目需求、README 和部署文档。

## 变更

- 北京时间每月 2 日 08:00 启动上一完整月份的终版刷新，成功后发布到公开报表。
- `opay-excellent-creatives-final.timer` 使用 `OnCalendar=*-*-02 08:00:00 Asia/Shanghai`、`AccuracySec=1s`、`RandomizedDelaySec=0`，保留 `Persistent=true`。
- 停用 `opay-excellent-creatives-initial.timer`，仓库移除其定时器文件。生产保留已停用的原 unit 便于回滚；人工 CLI 的 `initial` 阶段仍可使用。
- 本次只调整调度，不执行历史重算。刷新脚本、查询、素材规则、终版冻结和公开数据保持原实现。

## 发布前基线

- 主机：`43.166.187.96`，时区 `Asia/Shanghai`，systemd 239。
- 运行代码：`295b14d162b85a397e02dfcdbd637ef4a497e4b5`，路径 `/opt/opay-excellent-creatives/current`。
- 数据版本：`20260905T100638281465+0800`，最新月份 `2026-08` 终版。
- 旧任务：3 日 10:00 初版、5 日 10:00 终版，均 enabled/active；刷新 service 均 inactive，最近运行均成功。

## 发布与验证

1. 本地部署契约检查通过，提交并推送 `codex/opay-monthly-schedule-20260915`，服务器 fetch 精确提交到新的 release。
2. 确认数据盘已挂载且可写、两个刷新 service 均无在途任务、生产版本和公开数据未漂移。备份路径固定为 `/mnt/data-disk/opay-excellent-creatives/backups/20260915-monthly-schedule-0800`；如已存在则停止，不能覆盖。
3. 备份原两个 timer、current 目标、timer 与 service 状态、公开 HTML/latest 的 SHA-256，并校验备份字节。
4. 运行服务器 `systemd-analyze calendar` 和 `systemd-analyze verify`，确认新计划的下一次为 `2026-10-02 08:00:00 CST`。
5. 停用初版 timer、停止终版 timer；再次确认无在途刷新。原子切换 current，安装新终版 timer，`systemctl daemon-reload`，只启用并启动终版 timer。
6. 验证终版 timer enabled/active、初版 timer disabled/inactive，`NextElapseUSecRealtime` 为北京时间 `2026-10-02 08:00:00`；公开页面与 latest JSON HTTP 200，HTML/latest SHA-256 不变。确认没有在途刷新，journal 没有本次新增的刷新运行记录，持久 timer 时间戳不变；仍被 systemd 加载的 service 最近运行时间应与基线一致，已卸载 service 的时间戳可能为空。

终版 timer 的 Persistent 时间戳在 2026-09-05，晚于新计划本月的 2026-09-02，因此本次迁移不应补跑本月；上线后仍须用 service 状态和数据哈希确认。

## 精确回滚

仅在两个刷新 service 均 inactive 时执行；如有在途任务，等待自然结束。本次回滚不恢复数据、缓存或公开文件。

```bash
set -eu
opay_backup=/mnt/data-disk/opay-excellent-creatives/backups/20260915-monthly-schedule-0800
systemctl stop opay-excellent-creatives-final.timer opay-excellent-creatives-initial.timer
for opay_stage in initial final; do
  test "$(systemctl show -p ActiveState --value opay-excellent-creatives-refresh@${opay_stage}.service)" = inactive
done
install -m 0644 "$opay_backup/opay-excellent-creatives-initial.timer" /etc/systemd/system/opay-excellent-creatives-initial.timer
install -m 0644 "$opay_backup/opay-excellent-creatives-final.timer" /etc/systemd/system/opay-excellent-creatives-final.timer
opay_old_current=$(cat "$opay_backup/current-target.txt")
test "$opay_old_current" = /opt/opay-excellent-creatives/releases/295b14d162b85a397e02dfcdbd637ef4a497e4b5
test ! -e /opt/opay-excellent-creatives/.rollback-monthly-schedule
ln -s "$opay_old_current" /opt/opay-excellent-creatives/.rollback-monthly-schedule
mv -Tf /opt/opay-excellent-creatives/.rollback-monthly-schedule /opt/opay-excellent-creatives/current
systemctl daemon-reload
systemctl enable --now opay-excellent-creatives-initial.timer opay-excellent-creatives-final.timer
systemctl list-timers --all 'opay-excellent-creatives-*' --no-pager
```

历史快照、数据盘文件及现有公开结果全部保留。服务器验收证据写入同一备份目录的 `deployment-result.json`。
