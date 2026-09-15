# 部署与回滚

主机43.166.187.96；主API/root/drama_material_service、drama-material-api.service；静态同步/usr/share/nginx/html。

GitHub-first：本地验证→提交推送→服务器精确检出→真实只读索引probe→基线哈希核对及在线SQLite备份→排空受影响任务→切换代码/双份静态→仅重启主API→公网/原任务只读检查。

工具：scripts/deploy_meta_asset_recovery.py prepare RELEASE；apply BACKUP；rollback BACKUP。仅选定6个feature文件与HTML/JS/CSS。校验当前内容与GitHub基线e18d88f，漂移停止；回滚保留全部当前任务和索引数据，绝不恢复旧任务SQLite覆盖进度。主app和共享导航不变。

数据盘：/mnt/data-disk/fb-ad-asset-delete/video-reference-index；UUID须正确。索引不完整或过期时保持阻止；默认源构建/快照窗口600秒，单连接FIFO Gate，30GiB索引上限及2GiB剩余空间门槛。没有新增timer。

只读性能探针：python3 scripts/meta_video_reference_probe.py --job-id <已存在任务>。报告写数据盘reference-probe.json；不导入app、不调用Meta删除、不修改任务对象。

实际commit、备份目录、性能数据和验证结果在完成后追加。
