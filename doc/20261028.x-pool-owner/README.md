# X 定时池按入池人归属

素材池、剧目池定时任务以前没有读取池记录的创建人，冻结归因走默认管理员，导致历史内容效果和当日发布量计在管理员名下。本次从队列冻结的池记录 ID 读取 `created_by_user_id`，并校验素材 ID 或剧目 ID 一致；模板与手动任务仍分别按模板创建人、提交人归属。

新链接采用实际入池人的已有人员映射。历史修正仅更新 `x_post_link_attribution.context_json` 的人员及池来源字段，保留 URL 所用 `sub_user_id`、campaign ID、发布时间、短链、帖子、发布状态和支付记录。不会重写既有 URL 或补发帖子。历史系统恢复账号没有可确认的人名时，以“待归属（系统或缺失入池人）”单列，不能回退到管理员。

## 验证

`tests/test_x_pool_ownership.py` 覆盖两个池、精确来源校验、租户歧义、缺映射时不转移人员、历史应用/重复运行/回滚、系统账号待归属及来源变化时原子退出。`tests/test_social_daily_correction.py` 覆盖预览后发送、冻结内容、重复抑制与不明发送结果不重试。现有 X 归因、发布、日报、订单账单、卡片和社媒报表回归同时运行。

工作分支合入当前线上日报的账单采集文件及相应测试，以免维护来源回退到旧收入口径；部署脚本不覆盖这些已经存在且未变的运行文件。

## 部署

将提交推送 GitHub，在 CPU 的数据盘从 GitHub fetch 并建立该提交的干净 checkout。使用该 checkout 运行：

```bash
python3 scripts/deploy_x_pool_owner.py prepare
python3 scripts/deploy_x_pool_owner.py apply --backup <prepare 返回的目录>
```

脚本校验旧文件摘要、真实挂载 UUID、源提交及空间，暂停原本运行的相关触发器并等待已有任务退出，取得原有发布锁和日报锁后生成最终清单。在线 SQLite 备份上先验证应用、重复执行及回滚，再执行线上人员元数据事务；校验队列、发布日志、Repost 账本全量指纹不变。只重启主 API，X Sidecar 和 Auto 服务保持运行，最后恢复触发器原状态。超过等待时间会退出并恢复触发器，不终止正在执行的发布。

生产清单、数据库备份和人员明细只保存在私有数据盘目录，不能提交 Git。

## 日报更正

```bash
python3 scripts/social_daily_correction.py --date YYYY-MM-DD --revision x-pool-owner-v1
python3 scripts/social_daily_correction.py --date YYYY-MM-DD --revision x-pool-owner-v1 --send
```

预览先冻结 report/cards，发送复用完全相同的内容。更正版使用独立的修订目录、账本及 UUID 键，明确标记归属更正；原报表和原投递回执保留。发送需用户明确授权，不由定时器调用。返回不明结果时不得更换 revision 绕过保护。

## 回滚

```bash
python3 scripts/deploy_x_pool_owner.py rollback --backup <同一备份目录>
```

只对当前仍等于清单 after 值的归因记录恢复 before 值，并恢复本次代码；保留新发布、账号授权、所有业务账本和已发送更正版。回滚不覆盖 SQLite 整库，也不撤销已经发送的消息。后续新创建的正确归因不属于历史清单，不倒退为管理员。

## 2026-10-28 生产验收

- CPU：`43.166.187.96`。主 API `/root/drama_material_service`，X 运行目录 `/opt/x-post-automation/current`，日报运行目录 `/opt/youtube-publisher-daily-report/current`。
- 归因部署：`39a6956be3c5d6d300821b1fa59284b1e5ce7b1e`，历史清单 3,809 条。全部处理完成，随后只读生成的新清单为 0 条。20 条系统恢复入池记录明确待归属。
- 迁移前后发布队列、发布日志、Repost 账本全量指纹一致；应用、重复执行、回滚先在在线备份副本完成演练。只重启主 API；X Sidecar、Auto PID 不变，触发器恢复原状态，日报定时器 active。
- 原日报 JSON 摘要不变。2026-10-27 UTC 更正版收入 208,150 美分、成功发布 475 条，与原报相同；重新采集补入退款 3,846 美分并独立展示。更正版已经一次发送并回读核验；同 revision 再执行返回 already_sent。私有回执与人员明细位于数据盘 corrections 目录，不提交 Git。
- 线上 X 全员 API/CSV 完整返回，收入 130,259 美分、发布 166 条，各人员收入和发布数与更正版日报一致。初始完整查询因每 100 个历史 campaign 重建 SQL 连接耗尽 40 秒预算；单日改为每批 1,000 个，多日保留原每批 100 个及 14 天窗口，权限、精确 ID、缺失值、任何批次失败不缓存部分结果的规则均保留。上线后全员单日查询约 20 秒完成。
- 查询调整部署：`cbf8d9b49b22c5ed34bdac99d8a43b78b29e826d`，仅覆盖主 API 的 `features/social_analytics/scoped_metrics.py`。`52b5dd9` 是维护分支与现有日报冻结 catalog 接口的兼容整合，不覆盖现有主 API routes 或日报的账单采集运行文件。
- 本地相关回归 144 项通过，服务器查询/范围回归 23 项通过；此前归因部署版本服务器回归 127 项通过。完整日志和验收文件在私有审计目录。

归因备份：`/mnt/data-disk/social-analytics/backups/20261028T034533Z-pool-owner-39a6956be3c5`。查询备份：`/mnt/data-disk/social-analytics/backups/20261028T040518Z-single-day-cbf8d9b`。

归因精确回滚：

```bash
cd /mnt/data-disk/social-analytics/releases/39a6956be3c5d6d300821b1fa59284b1e5ce7b1e
python3 scripts/deploy_x_pool_owner.py rollback --backup /mnt/data-disk/social-analytics/backups/20261028T034533Z-pool-owner-39a6956be3c5 --wait-seconds 600
```

查询调整可独立回滚。先检查当前文件摘要仍是本次 after 值，避免覆盖后续版本，再恢复备份并窄重启：

```bash
python3 - <<'PY'
import pathlib, json, hashlib, shutil, subprocess
b = pathlib.Path('/mnt/data-disk/social-analytics/backups/20261028T040518Z-single-day-cbf8d9b')
r = json.loads((b / 'deployment.json').read_text())
p = pathlib.Path(r['target'])
assert hashlib.sha256(p.read_bytes()).hexdigest() == r['after']
assert hashlib.sha256((b / 'scoped_metrics.py').read_bytes()).hexdigest() == r['before']
shutil.copy2(b / 'scoped_metrics.py', p)
subprocess.run(['systemctl', 'restart', 'drama-material-api.service'], check=True)
subprocess.run(['systemctl', 'is-active', 'drama-material-api.service'], check=True)
PY
```
