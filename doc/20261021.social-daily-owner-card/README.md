# 社媒日报按人、渠道展示

2026-10-21 用户要求：只以 campaign ID 核实用户归属，按人和渠道分组，去掉“待核实”占位，私聊郜远发送一份新版。

归属继续使用全局冻结的 `(platform, site_id, campaign_id)` → 发布/短链账本；名称、`af_channel` 和裸任务 ID 都不能补归属。原有唯一 32 字符 YouTube 别名规则不变。源数据没有 campaign ID 或没有对应发布记录时，列为“未匹配发布记录”，不计入个人及汇总业绩。

卡片顺序：日期口径、核心指标、按收入排序的每人小计及渠道明细、渠道汇总、未计入业绩数据、指标说明和明细链接。使用飞书原生字段、分割线、备注和按钮；每个人的块不跨卡拆分；仍校验发送 JSON 的字节数。

日报采集对失败数据源最多重读一次，沿用只读端点、SQL FIFO Gate 和查询行数上限，整个采集使用 120 秒预算。网页请求不增加重试。未收齐源数据或必填指标缺失时，禁止冻结/发送正式业绩卡；预览仅显示采集失败说明，不展示部分合计。

私聊通过 `social_daily_private_preview.py` 根据后台用户 ID 查询唯一飞书 open_id；独立 `private-previews/<preview-id>/<user-id>/delivery.sqlite3` 和稳定 UUID，复用正式 `run.lock`。保留原群报、回执、正式投递账本和全部发布任务。重复发送返回 `already_sent`，sending/unknown 阻止重试。

验证：`python -m unittest discover -s tests -p 'test_social*.py' -q`；语法编译与 `git diff --check`；同一冻结快照逐指标核对 人员=渠道=已归属合计，以及 源数据=已归属+未匹配+排除；桌面/390px 预览检查。

部署先推送 GitHub，再在 CPU 数据盘干净工作树检出完整 SHA，运行 `scripts/deploy_social_daily_owner_card.py --sha <SHA>`。脚本校验旧版摘要、备份正式投递账本、保留旧 release，并只覆盖独立日报的六个文件。先从 staged release 生成预览并私聊回读，验收后 `--activate <deployment.json>`。不重启主 API 或发布 worker；既有 11:00 定时器在下次启动时读取新链接。

回滚：`python3 <source>/scripts/deploy_social_daily_owner_card.py --rollback <deployment.json>`。仅恢复代码链接，不恢复历史数据库、不撤回消息、不重发历史。
