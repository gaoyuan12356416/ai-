# 部署与回滚

服务器43.166.187.96，复用ai-image-daily.service/timer。新state=/mnt/data-disk/ai-image-daily-v3；旧state完整保留。当前指针/opt/ai-image-daily/current，release=/mnt/data-disk/deploy/ai-image-daily/releases/<SHA>。

1. 本地测试/JS/Python3.9 AST/diff检查，通过后提交并push codex/ai-image-three-products-20261022。
2. CPU确认数据盘UUID3e8ac4e8-7770-456d-9e89-2ec5dd405fa8、至少1GiB余量。新bare镜像/mnt/data-disk/deploy/ai-image-daily/repo.git，GitHub fetch精确分支；archive仅ops/ai-image-daily及本需求文档至对应SHA目录。逐blob校验。
3. 在candidate release执行Linux单测，report.py --prepare-only --state /mnt/data-disk/ai-image-daily-v3。此步骤仅查源数据与缓存缩略图，不写公开对象、不切服务。准备文件和manifest永远保留，后续不重新查询。
4. 保存旧state/history与COS旧daily校验清单；核对三产品/七日真实结果、旧版同范围差异和首屏包大小。停本日报timer，确认service闲置，deploy.py --repo <bare> --expected-commit <SHA>安装（不自动启动）。
5. 启动ai-image-daily.service，自动恢复冻结pending发布；先资源、日页、history、latest、最后旧首页跳转。公开读回原文/压缩字节及headers。成功后enable --now唯一timer，检查下次北京时间09:00及有限补偿。
6. 浏览器桌面/390px、筛选CSV、三次冷启动；保存acceptance.json和真实部署receipt。所有source DB、素材入库消费者、视频/广告发布服务保持原状态。

## 回滚

先停止本日报timer/service。用本次release的report.py --restore-public <v3-state/backups/run>恢复有旧版本的可变对象，必须通过完整哈希及metadata漂移守卫；首次新建的v3对象保留，历史/版本资源不删除。随后本次deploy.py --rollback <deploy-backups/stamp>恢复旧unit与代码指针；确认旧服务state不变后systemctl enable --now ai-image-daily.timer。不得恢复/覆盖任何业务数据库或重发素材。

实际SHA、安装备份、发布备份及验收路径写入test-report.md。

