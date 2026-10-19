# 两个Mobo产品消耗阈值调整

2026-10-19 用户要求将Moboreels/MoboShort阈值从3000改为1000并重跑昨天。延续严格大于语义：SUM(spend)>1000，不限制AF D0 ROAS。DramaWave保持消耗>=1000且ROAS>=45%，FreeReels不变。

仅修改features/screenshot_products.py中两个Mobo AUTO_PROFILES的min_spend。产品映射、封面来源、生成/上传、去重与失败隔离、10:00/22:00定时不变。原六图验收结果继续有效，不重复提交它们。

离线25项测试通过，覆盖消耗恰好1000排除、1000.001入选、ROAS零/低值入选，以及DramaWave双边界与历史全状态去重。

部署从GitHub精确提交复制该模块，验证除两个常量外源码字节完全相同；持共享cron锁，备份模块、deployment.json、crontab和SQLite。只替换模块，不重启API或生成worker；自动runner每次新进程读取新profile。维护原完整回滚清单中的模块指纹并单独记录profile_commit，不覆盖主API部署版本。

本次只重跑两个Mobo profile的2026-10-18完整北京自然日。所有已存在任务继续跳过，不重试历史失败，运行产物和任务库回读保存在CPU私有任务目录。生成批次交既有后台处理。

阈值回滚：持auto_cover_synthesis.lock，核对当前模块SHA与threshold-1000-deployment.json一致，再恢复其backup目录中的screenshot_products.py，并同步模块部署指纹；不恢复SQLite，不删除或重做任务与图片。

## 部署和运行结果

17:38:20北京时间部署f33cd6b0c61e7cdd802e441b85d723095951e36e，模块SHA256=cc384f59171d26cb90048903d1c3daf2ca4df3732a0bf63611bc7c815c5d30b4。备份before-mobo-spend-1000-20261019-173819。主API及四个图片worker PID全部未变；后续10:00/22:00自动使用新规则。

实际分别执行--profile moboreels和--profile moboshort，日期2026-10-18，两个退出码均0：

| 产品 | 达标候选 | 新增任务 | 跳过已有 | 提交失败 |
| --- | --- | --- | --- | --- |
| Moboreels | 2 | 1 | 1 | 0 |
| MoboShort | 0 | 0 | 0 | 0 |

新增Moboreels剧83878501，消耗1021.20、AF D0 ROAS28.21%，证明没有继承DramaWave的45%限制。原封面按本产品资源验证后创建任务6d99a322d4dd4bac8a92d11bf9f8fa3a；同产品剧ID唯一持久化，后续生成/上传交现有后台。私有mobo-spend-1000-20261018*保存出站intent、分产品运行记录、退出码、合并结果和任务库回读。

被跳过的Moboreels剧84322322，消耗1062.26、ROAS23.75%，对应之前图像内容审核失败任务da84501ca7ae40f19458892970f96f34；按用户确认的全状态去重规则跳过，没有重试。17:40新增任务快照为processing_cover/progress38，后台正在生成三种尺寸；这不是已完成上传的声明。
