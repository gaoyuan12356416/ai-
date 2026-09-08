# AI 后台 TT / FB / X Post 日报

每天北京时间10:00，由CPU服务器43.166.187.96的 `post-daily-report.timer`
触发独立程序，发送到 `oc_7c683c5770aef2e6c84a456e52cad389`。
范围仅含AI后台的自动模板、素材池、剧集池、手动立即和一次定时；旧版独立系统和平台端自行发布排除。

## 口径

- 统计日用北京时间计划时隙，绝不用提前创建的run时间替代计划时间。
- 昨日应发：冻结原始目标账号/Page，包含无素材、预检失败、零队列及部分容量的缺口。
- 昨日已发：昨日计划、昨日24点前由最终账本确认成功。
- 补发：昨日计划、次日00:00到10:00前确认成功。后续完成不会倒填按时完成。
- 往期计划昨日完成单列。X桥接、补偿子批次和Premium中继原帖不重复扩大原计划或目标交付数。
- submitted/source_published/unknown并非最终成功。FB没有平台发布时间，用系统确认时间并在卡片标注。
- 缺少历史目标快照时应发为null/未知，不能作零、不能给完整完成率。
- 原因使用历史错误证据；当前库存/前序任务推断明确标注，不伪装历史事实。

## 程序与数据

入口：`scripts/post_daily_report.py`；适配器：`features/post_daily_report/{tt,fb,x}.py`。
默认源库路径在入口 `DEFAULT_PATHS` 中，全部以SQLite `mode=ro` 读取；不导入发布器初始化，不查询旧版MySQL。
查询带只读事务、2秒锁等待和25秒数据库查询预算。任一渠道失败仍发送其余渠道并标明数据缺失。

CLI：

```bash
python3 scripts/post_daily_report.py --date 2026-09-07 --preview
python3 scripts/post_daily_report.py --date 2026-09-07 --send
```

默认日期是昨日；发送前要求统计截止10:00已经到达。
正式发送、预览与回执分别存入 `/mnt/data-disk/post-daily-report/reports/<date>/`、
`previews/<date>/` 和 `reports/<date>/receipt.json`。
数据盘UUID必须为 `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`；缺挂载时拒绝写入。

## 审计改动

TT旧素材排期新增 `tt_post_daily_schedule_audit` 和INSERT/UPDATE触发器。
X素材/剧集固定排期新增 `x_post_schedule_config_audit` 和INSERT/UPDATE触发器。
两者记录配置更改时间，初始化只记录当前观察，不伪造旧历史；数据库触发器可在线激活，无需重启发布器。
自动模板沿用现有模板版本和启停事件。

FB新增 `fb_auto_due_target_snapshot`：在素材/授权预检前保存时隙首次Page目标集合，空集合与无快照区分。
更换的FB core来自精确生产基线d2a6e91；只增加审计，保持发布门禁和执行契约。
FB需排空入口后窄重启sidecar才能加载该方法。不能以整份本工作树替换任一发布器。

## 发送与故障处理

复用服务器飞书机器人配置 `/root/.codex/plugins/feishu/config.json`，不复制或输出凭据。
`delivery.sqlite3` 以统计日期+chat_id唯一；发送前持久化sending状态，以固定UUID请求。
成功必须 `code=0` 且有message_id；保留回执。重复触发跳过sent。
明确拒绝最多3次并保留UUID；超时、连接异常、HTTP5xx或缺失message_id进入unknown，不自动重试。
进程中断遗留sending同样需要人工核对。禁止清除unknown后盲发。
群信息查询权限与群消息发送权限分开判断。
程序没有发布、补发、停账号、改规则、重启业务服务的能力。

```bash
systemctl status post-daily-report.timer post-daily-report.service
journalctl -u post-daily-report.service -n 60 --no-pager
```

卡片约束：聚合重复说明、限制长文本；超大时改发汇总卡，完整明细保留JSON。
正式卡片保存的紧凑JSON就是发送content，避免大小和摘要偏差。

## 验证与部署

```bash
python3 -m unittest discover -s tests -p 'test_post_daily_report*.py'
python3 -m compileall -q features/post_daily_report scripts/post_daily_report.py
git diff --check
```

2026-09-07已只读核对：TT64/48、FB50/50、X129/63；X往期昨日完成5。
部署先推GitHub准确提交，服务器fetch同一提交并核对文件SHA256；生产基线漂移时停止替换。
备份源文件、相关unit状态和SQLite在线备份到数据盘。审计表只做增量创建，不覆盖业务行。
上线顺序：审计备份/激活、代码校验、非发送预览、正式首报回执、重复发送去重验证、开启10点timer。

回滚：停用 `post-daily-report.timer`，恢复备份的TT/X/FB受影响源文件；
FB按相同排空流程恢复core并重启sidecar。可删除命名的TT/X审计触发器以停止后续采集，保留审计表。
绝不恢复旧SQLite备份覆盖新发布事实。独立报表和delivery账本保留供审计。
具体提交、备份目录、unit恢复状态和首报回执见部署验收记录。

本需求的运行约定以此文档维护；个人记忆未修改。
