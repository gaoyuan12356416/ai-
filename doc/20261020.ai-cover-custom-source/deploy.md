# 部署与回滚

GitHub-first：提交精确SHA并在CPU检出验证，生成release。新共用数据固定/mnt/data-disk/ai-cover-material-source；确认挂载UUID和余量后使用。主API部署仅替换一个回调函数、安装模块；YT和日报只切各自release。先canary后切换，保留原服务/timer配置、图片workerPID及源SQLite。

切换T必须冻结；先阻止旧新出站交叉，等待旧请求结束，保存原SQLite在线备份及文件hash。正式回滚不得让历史新接口attempt再进入旧接口：先HOLD共用出站并停封面同步timer，保留入库桥接/账本，不恢复任何数据库。回滚点和已执行精确命令以生产deployment.json和最终test-report为准。
