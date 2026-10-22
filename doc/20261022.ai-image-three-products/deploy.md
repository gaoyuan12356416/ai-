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

## 本次精确版本与回滚命令

已安装运行版本 `60c4e7c02da1db0ecfddb4feea31ce8a162ed0c2`。安装备份为 `/mnt/data-disk/deploy/ai-image-daily/backups/20261022T024858.819262Z`，发布备份为 `/mnt/data-disk/ai-image-daily-v3/backups/20261022T103659212933`。旧代码指针为 release `8424ded609372ca12709fca60e5b0a1bd88cf096`。

下面是需回滚时执行的命令，本次上线未执行回滚。先停止唯一日报调度，恢复旧固定入口，再恢复代码与 units；恢复操作带哈希和元数据漂移检查：

```bash
systemctl stop ai-image-daily.timer
systemctl stop ai-image-daily.service
python3 -B /mnt/data-disk/deploy/ai-image-daily/releases/60c4e7c02da1db0ecfddb4feea31ce8a162ed0c2/ops/ai-image-daily/report.py \
  --state /mnt/data-disk/ai-image-daily-v3 \
  --env-file /root/drama_material_service/.env \
  --restore-public /mnt/data-disk/ai-image-daily-v3/backups/20261022T103659212933
python3 -B /mnt/data-disk/deploy/ai-image-daily/releases/60c4e7c02da1db0ecfddb4feea31ce8a162ed0c2/ops/ai-image-daily/deploy.py \
  --rollback /mnt/data-disk/deploy/ai-image-daily/backups/20261022T024858.819262Z
readlink -f /opt/ai-image-daily/current
systemctl cat ai-image-daily.service ai-image-daily.timer
```

确认返回旧 release、旧 state 与原 09:00/09:10/09:30 配置，且 `restore-receipt.json` 校验成功后，再执行 `systemctl enable --now ai-image-daily.timer`。首次上线前不存在的新 v3 首页、历史页与版本资源会保留；旧固定入口恢复旧版内容。不得删除历史资源或覆盖业务数据库。若任一漂移检查失败，保留现场并先核对后续发布，不绕过校验。

全部实测结果见 `test-report.md`；服务器验收记录为 `/mnt/data-disk/ai-image-daily-v3/acceptance.json`。

