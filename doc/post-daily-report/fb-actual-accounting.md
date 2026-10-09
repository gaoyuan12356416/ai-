# FB 实际发布口径

FB 日报使用 `actual-fb-v2`。只读取发布库，不创建计划、补发帖子或更改发布器。

## 计数

- 自动应发依据运行时冻结的 Page 集合、`config_json`、Page 每日频次和本时隙计算。随机计划必须有同模板版本、同日期的冻结时刻。缺少历史配置或 Page 集合时应发为未知；不读取当前模板替代历史配置。
- 频次外位置从应发中排除，包括被更早的历史未知结果检查遮住的频次外位置。单列 `frequency_excluded`，不计失败。
- 手动应发使用操作时冻结的目标集合，不按自动五个时隙扩张。
- 成功要求任务与账本均为 `published`、Page 和平台 ID 一致、无 `unknown_outcome`，且确认时间早于截止。统计日成功和次日 10:00 前补发分开计数，二者合计为“确认成功”。
- 明确失败为已确认失败的发布任务。`skipped` 中的前序未知结果、过期、无候选等单列受阻；执行中或平台处理中单列待处理。
- 同剧/同素材冷却等正常策略跳过单列 `policy_skipped`，包含于应发，不计发布失败。
- 未知、成功凭证不一致、截止后确认等单列待确认，不计成功或失败。
- 各状态必须满足：应发 = 当日成功 + 补发 + 明确失败 + 受阻 + 待处理 + 待确认 + 策略跳过。否则该渠道校验失败，不显示虚假的零值。

错误原因保留已记录的尝试证据：最后一个 Token 的 `190` 不得覆盖同任务的 `389` 视频 URL 抓取失败。超时原因区分已制作但被未知任务阻塞，以及未制作完成反复延后；不凭泛化错误臆测底层转码原因。

## 更正播报

`python3 scripts/post_daily_report_correction.py --date YYYY-MM-DD --channel FB --revision <stable-id> --preview|--send`

必须有原日报 `sent` 和一致的回执；保持原统计日及次日 10:00 截止，归档 JSON 中保留原 TT/X 对象，飞书更正卡只展示 FB。原日报、回执、投递账本不覆盖。独立 revision/UUID/投递账本保证重复执行 `already_sent`；`sending/unknown` 禁止盲目重发。

## 部署与回滚

在 GitHub 精确提交的 `/mnt/data-disk/post-daily-report/releases/<sha>` 运行 `scripts/install_post_report_fb_actual.py`，先预检后 `--apply`。安装器要求基线 `9b9baf7882bcce3a9e0b199c3a8697d0027d322a` 和源文件/日报 unit 哈希一致，验证数据盘，锁定日报、备份旧代码和日报投递库，再仅切换日报 `current`。保留原 timer 启用状态，不修改或重启发布器。

运行全部 `test_post_daily_report*.py`，以只读线上更正预览核对状态守恒、原 TT/X 和原日报哈希；正式发送后 GET 读取飞书消息、核对内容和群，并再次运行相同发送命令验证 `already_sent`。

回滚仅执行本次备份目录的 `rollback.sh`，恢复旧日报代码。不得恢复旧发布数据库或投递账本，不得删除已发送更正。
