# 部署文档

## 变更内容

- 新增独立公开静态路径 `/reports/opay-excellent-creatives/`。
- 新增 `/opt/opay-excellent-creatives/releases/<commit>` 与 `current`。
- 新增数据盘缓存/快照/缩略图和公开静态目录。
- 新增初版、终版两个月度 timer；不修改旧报表 unit。

## 配置项

生产环境文件 `/etc/opay-excellent-creatives.env`，`0600 root:root`。配置仅保存路径、超时和媒体允许主机等非公开运行参数；MySQL 凭据继续由现有本机只读命令模块提供，禁止进入 GitHub/日志。

## 数据库变更

无 MySQL DDL/DML。所有查询必须在 `101.32.56.53:63350` 验证 `@@read_only=1` 后执行；写端口 63353 不在本需求范围。

## 部署步骤

1. 本地完成编译、单元、前端契约和 `git diff --check`。
2. 提交并推送分支，记录精确 GitHub commit。
3. 在服务器验证 `/mnt/data-disk` 为 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8` 的已挂载可写文件系统并检查空间。
4. 验证 GitHub SSH，fetch 精确 commit 到新的不可变 release；不得从本地直接复制源代码伪装成 GitHub 发布。
5. 在 `/mnt/data-disk/opay-excellent-creatives/backups/<timestamp>-pre-<sha>` 备份旧 current、公开提交点、Nginx、env 和 units，生成并校验 SHA-256 清单。
6. 安装新 env、Nginx 和 systemd 文件，执行服务器端编译/测试。
7. `nginx -t` 成功后 reload Nginx；先不启用 timer。
8. 使用影子输出完成 2026-07 回归和媒体抽样，再回填 `2026-01` 至最近完整月份。
9. 检查每月快照后原子发布，最后切换 `current` 和 `latest.json`。
10. 启用并启动两个 timer，完成公开、旧系统和只读抽样验收。

## 验证步骤

```bash
python3 -m py_compile /opt/opay-excellent-creatives/current/ops/opay-excellent-creatives/opay_excellent_creatives.py
python3 -m unittest discover -s /opt/opay-excellent-creatives/current/ops/opay-excellent-creatives -p 'test_*.py' -v
python3 /opt/opay-excellent-creatives/current/ops/opay-excellent-creatives/validate_frontend_contract.py
sqlite3 /mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives.sqlite3 'PRAGMA quick_check;'
nginx -t
systemctl status opay-excellent-creatives-initial.timer --no-pager
systemctl status opay-excellent-creatives-final.timer --no-pager
curl -sS -I https://ai.yingliangads.com/reports/opay-excellent-creatives/
curl -sS https://ai.yingliangads.com/reports/opay-excellent-creatives/latest.json
```

还需验证匿名 200/无 Location、robots、2026-01 起月份清单、Google 无估算、Meta/TikTok 非空、素材 ID/产品范围、公开 JSON 不含密码/Token、主 API和旧 AI Game Performance 行为不变。

## 回滚方案

1. 停止并禁用两个新 timer，确认 oneshot service 不在运行。
2. 将 `current` 原子切回备份记录的上一 release；若首次部署则移走新 current，不删除。
3. 恢复备份的 Nginx/env/units/公开 `index.html` 和 `latest.json`，清单最后恢复。
4. `systemctl daemon-reload && nginx -t && systemctl reload nginx`。
5. 保留 SQLite、快照和缩略图用于审计，不在常规代码回滚中恢复或删除数据盘事实。
6. 复核新路径不再提供本报表内容，并检查旧报表、主 API、Nginx 和磁盘；实际 HTTP 状态取决于父级 server 的既有兜底行为。

## 生产发布记录

- 发布时间：2026-08-26（Asia/Shanghai）。
- GitHub 分支：`codex/opay-excellent-creatives-report-20260826`。
- 运行提交：`0cba014b56f1c6394a9d0d3be5d735a370f83659`。
- 当前 release：`/opt/opay-excellent-creatives/releases/0cba014b56f1c6394a9d0d3be5d735a370f83659`；`current` 已解析到该目录。
- 首次部署，无前一 release。上线前备份：`/mnt/data-disk/opay-excellent-creatives/backups/20260826T185200+0800-pre-0cba014`；`manifest.txt` 与 `SHA256SUMS` 校验通过，记录 `pre_current/public/nginx/env/units=absent`。
- 数据版本：`20260826T184515557171+0800`，清单 SHA-256 `7b272cb2ea01e8a1ac9da0361d124ff517022bea0867a29cbf07cf3decb4d0dc`。
- 回填：2026-01 至 2026-07 全部为终版，共 186 行。瞬时单元 `opay-excellent-creatives-backfill-0cba014.service` 成功退出，`ExecMainStatus=0`。
- `opay-excellent-creatives-initial.timer`、`opay-excellent-creatives-final.timer` 均 enabled/active；下一次分别为 2026-09-03、2026-09-05 北京时间 10:00 左右（含 `RandomizedDelaySec`）。
- `nginx -t`、systemd unit verify、匿名 HTTP 200、noindex、版本化 JSON、旧报表 302 行为和主服务回归均通过。

## 首次部署精确回滚命令

本次上线前对应对象均不存在，因此回滚采用“移到保留目录”而不是删除，SQLite、快照、缩略图和不可变 release 均保留：

```bash
set -euo pipefail
opay_rollback_hold=/mnt/data-disk/opay-excellent-creatives/rollback-hold/20260826-first-release
install -d -m 0700 "$opay_rollback_hold"

systemctl disable --now opay-excellent-creatives-initial.timer opay-excellent-creatives-final.timer
test "$(systemctl is-active opay-excellent-creatives-refresh@initial.service || true)" = inactive
test "$(systemctl is-active opay-excellent-creatives-refresh@final.service || true)" = inactive

mv /etc/nginx/default.d/opay-excellent-creatives.conf "$opay_rollback_hold/"
mv /etc/opay-excellent-creatives.env "$opay_rollback_hold/"
mv /etc/systemd/system/opay-excellent-creatives-refresh@.service "$opay_rollback_hold/"
mv /etc/systemd/system/opay-excellent-creatives-initial.timer "$opay_rollback_hold/"
mv /etc/systemd/system/opay-excellent-creatives-final.timer "$opay_rollback_hold/"
mv /opt/opay-excellent-creatives/current "$opay_rollback_hold/current"
mv /usr/share/nginx/html/reports/opay-excellent-creatives "$opay_rollback_hold/public"

systemctl daemon-reload
nginx -t
systemctl reload nginx
curl -sS -o /dev/null -w '%{http_code}\n' https://ai.yingliangads.com/reports/opay-excellent-creatives/
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' https://ai.yingliangads.com/reports/ai-game-performance/
```

实际演练已执行 timer 的 disable/stop 与 enable/start 完整往返，恢复后两者均 enabled/active、页面仍为 200、`nginx -t` 通过。为避免人为制造公开报表中断，文件移动和 route 下线部分按已校验备份清单做命令级复核，未在生产实际执行。

## 注意事项

- 公开无鉴权是已确认产品决策；`noindex` 不等于保密。
- `latest.json` 必须最后切换。
- 最终月份冻结后禁止普通 timer 改写；修复历史数据必须显式 `--rebuild` 并保留差异审计。
- 缩略图/源文件异常不得阻塞整月；数据库、AF 配置或发布提交失败必须阻塞并保留旧版本。
