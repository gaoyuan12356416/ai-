# Mobo昨日消耗前5部

2026-10-19最新规则取代两个Mobo产品的>1000及此前>3000门槛。各产品查询前一个完整北京自然日、platform=0、按剧汇总的正消耗，不限制ROAS；校验本产品原封面可用，按未舍入SUM(spend)降序、content_id同额排序，排除本产品全部已有任务后取前5部。没有分集地址不影响Mobo封面资格。

10:00、22:00的每次运行分别最多提交5部；后续运行继续排除之前所有状态任务后选取，不设每日累计5部上限。不足5部照实际数量执行，零候选留记录。选择集合先冻结，拒绝或不确定提交不使用第6部补位；不确定请求停止该产品，另一产品继续。共享锁、任务库只读和失败时停止提交保持不变。

DramaWave保持>=1000且AF D0 ROAS>=45%，原有数量配置保持不变；FreeReels保持手动。Mobo忽略旧AUTO_COVER_LIMIT在去重前的截断，防止已有任务占满前5导致漏选。summary新增top_n、eligible_new_count、selected_count、deferred_by_top_n_count区分候选、排除、选择与暂缓。

Windows离线28项测试通过，覆盖正消耗、低消耗/零ROAS、未舍入排序及同额排序、全状态先去重后选5、其他产品同剧不阻止选择、提交失败不补位、零/不足5候选、数据库错误停止该产品、产品失败隔离、DramaWave原双边界与不限5部。

发布：先推送GitHub，在CPU acceptance-tools检出精确提交并执行Linux测试，运行ops/screenshot-mobo-products-20261019/update_selection.py。该安装器持既有cron锁，校验两个旧文件SHA、数据盘及空间，备份代码、部署状态、在线SQLite、crontab和前端文件，原子替换两个文件，并更新原完整回滚清单。API与图像worker无需重启，10:00/22:00时刻不变。

仅此次规则回滚：从本次精确Git提交执行 `python3 /mnt/data-disk/codex/tasks/screenshot-mobo-products-20261019/acceptance-tools/ops/screenshot-mobo-products-20261019/update_selection.py --rollback`。它持锁并核对文件未漂移后恢复>1000版本的两个文件和对应部署指纹；保留当前任务数据库、图片和入库记录，无服务重启。不使用备份数据库覆盖在线库。

昨日实际执行日期固定为2026-10-18，分别使用--profile moboreels和--profile moboshort。私有mobo-top5-20261018*记录一次性出站意图、每产品输出/退出码及任务库回读；后台继续生成和上传，不重复原六图验收。

## 上线与昨日运行结果

18:07:34北京时间部署758ea90ae6336be14984a67526f67e6d54430f0e，Windows/Linux28项测试通过。CPU43.166.187.96:/root/drama_material_service仅替换两个筛选文件；备份before-mobo-top5-20261019-180733，详细路径、前后SHA保存在top5-deployment.json。API及四个图像worker PID完全未变，cron/crond正常，四产品接口验证通过。

| 产品 | 正消耗且有原封面 | 排除已有任务 | 剩余可选 | 本次提交 | 其余未选 | 提交失败 |
| --- | --- | --- | --- | --- | --- | --- |
| Moboreels | 22 | 3 | 19 | 5 | 14 | 0 |
| MoboShort | 468 | 2 | 466 | 5 | 461 | 0 |

两次执行退出码均0。18:08:43只读回查线上任务库与部署前在线备份，确认10个job_id的产品和剧ID正确，按app_id+content_id分别仅1条、备份库均无该剧任务。同剧跨产品分别创建成功。快照为4条processing_cover和6条queued，生成及上传交既有后台，未将本批误报为已完成。

本次没有重跑DramaWave。正式入库的原两条验收任务和已有失败任务全部跳过，没有重复上传或恢复历史失败。技能上下文同步了最新top5语义、部署回滚和后台交接。
