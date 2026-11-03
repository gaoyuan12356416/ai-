# 部署与回滚

服务器43.166.187.96，运行目录/root/drama_material_service，代码源gaoyuan12356416/ai-，分支codex/drama-custom-material-upload-20261103。

从GitHub精确checkout后执行 `python3 scripts/deploy_drama_material_source.py --commit <sha>` 检查；加 `--apply` 安装。校验数据盘UUID、线上app/HTML/runtime SHA和共享入库模块，等待合成及封面任务空闲后备份并局部安装，重启drama-material-api.service，启用独立drama-material-source.service。同步运行目录和Nginx静态副本。

回滚：在该精确checkout执行 `python3 scripts/deploy_drama_material_source.py --rollback <deployment>/manifest.json`。核对当前代码和备份SHA，停用新增worker，恢复旧文件并重启主API。保留上传SQLite及全部素材回执，不恢复业务数据库，不重发已提交素材。若后续功能需要恢复，仍须读取原账本。
