# 2026-09-18 TT 日报修复及授权重发

已完成上线，并在北京时间 2026-09-18 10:33:30 向原日报群发送「TT 修正版」。统计日为 2026-09-17，补发截止仍为 2026-09-18 10:00。

## 结果

TT 应发 52、昨日已发 48、补发 0、未完成 4。随机叠加为 40/36/0/4，拼引导片尾为 12/12/0/0，人工素材排期及单次发布为 0/0/0/0。FB、X 完整渠道对象沿用原日报，未经重新采集或修改。

修复 TT Auto WAL 辅助文件权限、人工排期已结束启用区间误判，以及 TT 数据读取失败仍显示已发 0 的问题。修正版脚本仅在明确请求时运行，不加入 timer。

## 版本与部署

- GitHub：`gaoyuan12356416/ai-`，分支 `codex/post-report-tt-fix-20260918`。
- 代码提交：`8a0aad86d96722d61094176ee2f7be62de8f944e`。先本地提交推送，再由生产 bare repo 从 GitHub fetch 并核对 FETCH_HEAD；13 个发布文件逐一按 Git blob 验证。
- 服务器：`43.166.187.96`。
- 生效路径：`/opt/post-daily-report/current` → `/mnt/data-disk/post-daily-report/releases/8a0aad86d96722d61094176ee2f7be62de8f944e`。
- 旧版本：`/mnt/data-disk/post-daily-report/releases/31aefc70b913ac46c54fc5f636fee97a4a51252e`。
- 备份：`/mnt/data-disk/post-daily-report/backups/tt-fix-20260918T023202Z`，含旧 unit、日报源代码、原始报表、delivery 在线备份和 manifest。
- 仅切换独立日报 release 与 unit 并执行 daemon-reload。启动正式日报 service 返回 already_sent，Result=success、ExecMainStatus=0；未重启发布器或主 API。
- timer 仍 active，下一次为 2026-09-19 10:00 北京时间。

## 验证

- `python -m unittest discover -s tests -p 'test_post_daily_report*.py'`：55 项通过。覆盖历史区间边界、有效随机计划确实缺失、未知展示、原报告保留、修正版消息去重、未知发送阻止重试、回执和截止时间匹配。
- Python 编译与 `git diff --check` 通过，生产 `systemd-analyze verify` 通过。
- 独立小型 WAL 库在严格只读环境复现原错误。按新 unit 权限读取成功，同时 SQL 写入和主库文件写入均被拒绝；未使用 immutable 读取活动库。
- 候选版与正式发送均在 systemd 沙箱内运行。预览和发送卡片 SHA256 相同：`8416e285ec3198308c5d377f0f375f0d469f757513bcd5d0b3168b675f77101b`，请求预算 16047 字节。
- 飞书发送 code=0，随后 GET 消息回读 code=0、原 chat_id 精确匹配、interactive、deleted=false，包含 TT 52/48 数据。
- 修正版 message_id：`om_x100b65fe616804acc45cf6dabd5dcd5`。重复执行同一 revision 返回 already_sent，发送 attempts=1。
- 原日报的 report/card/preview/receipt 四文件 SHA256 未变，原 delivery 仍 sent、attempts=1。
- 更正归档：`/mnt/data-disk/post-daily-report/corrections/2026-09-17/tt-fix-20260918/`；含 report、card、preview、receipt、readback 和独立 delivery.sqlite3。

## 精确回滚

以下命令仅恢复日报代码与 unit，不撤回已发送消息，不恢复历史数据库覆盖当前状态：

```bash
flock -x /mnt/data-disk/post-daily-report/run.lock /bin/bash -c '
set -eu
ln -s /mnt/data-disk/post-daily-report/releases/31aefc70b913ac46c54fc5f636fee97a4a51252e /opt/post-daily-report/current.rollback-20260918
mv -Tf /opt/post-daily-report/current.rollback-20260918 /opt/post-daily-report/current
install -m 0644 /mnt/data-disk/post-daily-report/backups/tt-fix-20260918T023202Z/post-daily-report.service /etc/systemd/system/post-daily-report.service
systemctl daemon-reload
'
systemctl show post-daily-report.timer -p ActiveState -p NextElapseUSecRealtime
readlink -f /opt/post-daily-report/current
```

回滚会恢复旧版 TT 统计缺陷。保留原日报、更正版和全部发送状态，不通过清除 sent/unknown 强制再次发送。个人记忆未修改；AI 后台维护技能的 project-map 增补此次运行约定。
