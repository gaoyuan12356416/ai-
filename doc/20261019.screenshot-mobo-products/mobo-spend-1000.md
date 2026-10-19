# 两个Mobo产品消耗阈值调整

2026-10-19 用户要求将Moboreels/MoboShort阈值从3000改为1000并重跑昨天。延续严格大于语义：SUM(spend)>1000，不限制AF D0 ROAS。DramaWave保持消耗>=1000且ROAS>=45%，FreeReels不变。

仅修改features/screenshot_products.py中两个Mobo AUTO_PROFILES的min_spend。产品映射、封面来源、生成/上传、去重与失败隔离、10:00/22:00定时不变。原六图验收结果继续有效，不重复提交它们。

离线25项测试通过，覆盖消耗恰好1000排除、1000.001入选、ROAS零/低值入选，以及DramaWave双边界与历史全状态去重。

部署从GitHub精确提交复制该模块，验证除两个常量外源码字节完全相同；持共享cron锁，备份模块、deployment.json、crontab和SQLite。只替换模块，不重启API或生成worker；自动runner每次新进程读取新profile。维护原完整回滚清单中的模块指纹并单独记录profile_commit，不覆盖主API部署版本。

本次只重跑两个Mobo profile的2026-10-18完整北京自然日。所有已存在任务继续跳过，不重试历史失败，运行产物和任务库回读保存在CPU私有任务目录。生成批次交既有后台处理。

阈值回滚：持auto_cover_synthesis.lock，核对当前模块SHA与threshold-1000-deployment.json一致，再恢复其backup目录中的screenshot_products.py，并同步模块部署指纹；不恢复SQLite，不删除或重做任务与图片。
