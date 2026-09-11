# 部署与回滚

主机43.166.187.96；运行/root/drama_material_service；静态/usr/share/nginx/html。

脚本scripts/deploy_youtube_channel_cache.py复用备份/空闲/安装哈希校验。精确验证GitHub提交标记与生产文件基线；仅替换app.py中的YouTube handler，其他内容保持。改动文件备份与SQLite在线备份在数据盘。停止API及两个关联YouTube worker，统一writer保持运行；安装后恢复并健康检查。rollback参数指向本次backup目录，只恢复代码，保留所有任务、通知、人工频道核验审计、素材SQL与资产。

生产旧基线（同一登录用户、loopback HTTP）：topbar3.4ms/bootstrap3.5ms/tasks10.6ms/channels2644.5ms/materials冷4877.1ms/热3.9ms。这是服务器接口耗时，不代表用户网络与整页耗时。
