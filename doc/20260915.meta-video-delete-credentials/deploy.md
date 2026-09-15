# 部署及回滚

主机43.166.187.96；逻辑/root/drama_material_service，保持数据盘符号链接；公共静态/usr/share/nginx/html；仅重启drama-material-api.service。

流程：GitHub提交推送→服务端精确检出→Python3.9.6测试/编译→meta_video_credential_probe.py --job-id 已确认任务（只GET）→deploy_meta_video_credentials.py prepare RELEASE→排空发布timer/service和资产任务→apply BACKUP→重启主API→恢复原timer→公开/真实会话只读验收。

备份脚本核对1b86d2af基线，保存5个Feature模块和双份HTML/JS/CSS（11目标）、最新SQLite及哈希；无schema变更、无源库写入。新版本20260915-meta-video-page-credentials。

回滚先同样排空，执行本次release下scripts/deploy_meta_video_credentials.py rollback BACKUP，再systemctl restart drama-material-api.service。恢复原active timer并检查哈希/健康。保留现有台账、凭证秘密、回执、unknown锁，不恢复备份SQLite。

实际提交/release/备份/测试/只读验收部署后回填。
