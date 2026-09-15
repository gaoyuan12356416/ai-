# 部署及回滚

主机43.166.187.96；逻辑/root/drama_material_service，保持数据盘符号链接；公共静态/usr/share/nginx/html；仅重启drama-material-api.service。

流程：GitHub提交推送→服务端精确检出→Python3.9.6测试/编译→meta_video_credential_probe.py --job-id 已确认任务（只GET）→deploy_meta_video_credentials.py prepare RELEASE→排空发布timer/service和资产任务→apply BACKUP→重启主API→恢复原timer→公开/真实会话只读验收。

备份脚本核对1b86d2af基线，保存5个Feature模块和双份HTML/JS/CSS（11目标）、最新SQLite及哈希；无schema变更、无源库写入。新版本20260915-meta-video-page-credentials。

首次prepare检测到同期退役部署d697e30生成的HTML导航版本变更，正确拒绝覆盖，未创建备份或修改运行文件。核对两处HTML仅quick-nav版本改为20260915retired；本次源码已保留该变更，并将该生成HTML的SHA256（7ad4ce404ec8c9f9e06afaee61ac47c2e4480c729dd9a3bd6f1322315295c088）设为唯一允许的逐文件基线。其他10个目标仍严格匹配1b86d2af，不重启或恢复已退役模块。

回滚先同样排空，执行本次release下scripts/deploy_meta_video_credentials.py rollback BACKUP，再systemctl restart drama-material-api.service。恢复原active timer并检查哈希/健康。保留现有台账、凭证秘密、回执、unknown锁，不恢复备份SQLite。

## 实际上线记录

- 2026-09-15 17:12至17:16（Asia/Shanghai）。实现提交3e46d121a22d30680079afb44b8a889712e39840；最终运行提交b0d5790d3b5b637b27ff22cc53785cc4df82ea37，分支codex/meta-video-delete-credentials-20260915，GitHub推送及服务器精确fetch已确认。
- Release：/mnt/data-disk/meta-ad-asset-delete/release-b0d5790d3b5b。
- 备份：/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260915T091240Z-b0d5790d3b5b；11个目标、在线SQLite、plan.json、hashes.json完整；SQLite integrity_check=ok。
- 服务器Python3.9.6运行211项回归通过；最终版本与已测试版本的Feature模块/测试/只读Probe字节一致，变更后的部署脚本编译通过。
- 只读Probe共35次GET、0次DELETE，17个失败源Video全部匹配Page1151354758071142的候选用户709/凭证行13134；实际GET /me确认Page身份。原始上传者及删除权限不能由此推定。
- 保存并暂停9个原active发布timer；相关service自然空闲，资产执行/预览/重核为空。6个在途剧集任务由外部worker承载（DRAMA_JOB_USE_WORKER=1），使用safe_restart_drama_api.sh仅重启主API；保留外部worker、YouTube及已退役模块状态。
- 主API PID由3001597变为3017191，active/running；9个timer恢复原active。保留主目录数据盘符号链接；未重启其他服务。
- 11个目标与GitHub精确哈希一致；两处静态及公网版本同步，HTML/JS/CSS200且字节一致，topbar200、未登录products/jobs401；启动日志无异常标记。
- 原任务所有者有效会话GET通过，17个失败Video及52对象原结果不变；全部120条对象、63条尝试、30条回执、0条锁与备份逐行一致。本次未发起真实DELETE。
- 服务器证据：本备份目录内service-state-before.json、service-state-after.json、public-verification.json、authenticated-verification.json；只读凭证证据/mnt/data-disk/fb-ad-asset-delete/video-credential-probe-28c816534e0b49c0b9723af02158a3b0.json及video-credential-investigation-20260915.json（均不含Token）。

## 精确回滚

保存并暂停上述9个原active发布timer，等待相关service空闲，确认无资产执行/预览/重核，再执行：

```bash
python3 /mnt/data-disk/meta-ad-asset-delete/release-b0d5790d3b5b/scripts/deploy_meta_video_credentials.py rollback /mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260915T091240Z-b0d5790d3b5b
bash /mnt/data-disk/meta-ad-asset-delete/release-b0d5790d3b5b/scripts/safe_restart_drama_api.sh
```

恢复这次保存的active timer，检查服务/公网/双静态。代码退回1b86d2af及同期已退役导航版本；保留当前SQLite/回执/unknown锁，不恢复备份台账，不撤销退役部署。
