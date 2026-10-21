# 2026-10-21 部署与私聊验收

- 代码已推送 GitHub：`9b4f453620e04ad9c7a35eeedd82ae114842029b`，分支 `codex/social-daily-owner-card-20261021`。
- CPU：`43.166.187.96`。独立日报入口 `/opt/youtube-publisher-daily-report/current` 已切换到 `/mnt/data-disk/youtube-publisher-daily-report/releases/9b4f453620e04ad9c7a35eeedd82ae114842029b-owner-card`。
- 只切换独立日报代码链接。未重启主 API、社媒发布 worker；既有 timer 保持 active，下一次北京时间 `2026-10-22 11:00`。
- 备份/部署清单：`/mnt/data-disk/youtube-publisher-daily-report/backups/20261021T032401Z-owner-card/deployment.json`；包含旧 release 和代码摘要，目录内保存投递账本备份。

## 核验结果

1. `python -m unittest discover -s tests -p 'test_social*.py' -q`：161 项检查，159 通过、2 项已有环境条件跳过。语法编译和 `git diff --check` 通过。
2. 本地 900px、390px 预览人工查看；390px 的内容宽度同为 390px，没有横向溢出，无“待核实”。这属于本地卡片结构预览；实际飞书客户端外观由飞书渲染。
3. 冻结快照按每个指标校验：人员之和 = 渠道之和 = 已归属总计；源数据 = 已归属 + 未匹配 + 明确排除。
4. 部署版在 CPU 重新采集 2026-10-20 UTC，YouTube/X/Facebook 均 complete，结果与私聊冻结快照一致。
5. 已归属：收入 **$5,712.68**，成功发布 **298**，激活 **7,362**，点击 **63,559**，访问 **127,297**，付费人数 **406**，退款 **$12.99** 单列。
6. YouTube 恢复的收入为 **$613.89**。原 11:00 报告留存了读取失败状态，没有底层异常明细，不能据此断言是超时或排队；本次重新读取成功，并添加有界失败重读和完整性发送门槛。
7. 未匹配发布记录单列：YouTube 2 组，X 1 组，Facebook 22 组。Facebook 未匹配收入 **$4,593.51** 不纳入个人及汇总。不能把同值裸任务 ID、名称或渠道参数当作归属证据。

## 私聊投递

- 后台只读核验用户 `892fd2e8` 为郜远，open_id `ou_62fed22c4918524e48ded4eab8b82b1e`；使用 `receive_id_type=open_id` 发送。
- 飞书消息：`om_x100b62b749dc40b0c1b768b2267a88b`。
- 消息 GET 回读 code=0，日期、金额一致，未删除，无“待核实”；聊天 ID `oc_f28db2188e63be9367a9fe0123511b34`。群详情接口不支持该私聊读取，未将其作为重发依据。
- 重复执行返回 `already_sent`，没有第二次消息发送。
- 预览、receipt、readback 和独立投递账本：`/mnt/data-disk/youtube-publisher-daily-report/private-previews/owner-channel-20261021/892fd2e8/`。
- 原正式日报五个文件与正式 `delivery.sqlite3` 共六个文件 SHA256 未变；没有群内重发，也未更改发布任务和平台记录。
- AI 后台维护技能已更新并独立推送：`codex-personal-skills` 提交 `73c9131`，保留其他已有本地修改。

## 回滚

在 CPU 执行：

```sh
python3 /mnt/data-disk/social-analytics/owner-card-source-20261021/scripts/deploy_social_daily_owner_card.py --rollback /mnt/data-disk/youtube-publisher-daily-report/backups/20261021T032401Z-owner-card/deployment.json
```

只恢复旧代码链接，不恢复历史数据库、不撤回私聊消息、不重发历史日报。
