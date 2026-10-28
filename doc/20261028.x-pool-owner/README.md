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
