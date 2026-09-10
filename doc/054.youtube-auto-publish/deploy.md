# 部署与回滚

目标CPU `43.166.187.96`，项目 `/root/drama_material_service`，现有HK executor不变。仅部署manifest列出的变更文件，不用本分支整目录覆盖共享服务。GitHub exact commit上传与校验成功是部署前提。

## 配置
- `/etc/youtube-auto-publish.env`：参考仓库同名example，开关1，SQL指向独立文件。
- `/etc/youtube-auto-publish/material-source.sql`：当前仅注释（预留），不配置临时查询。最终SELECT列约定见requirements.md；改完文件20秒内刷新，无需改页面。
- `/mnt/data-disk/youtube-auto-publish/{assets,generation,media}`：新私有数据；必须确认挂载UUID=3e8ac4e8-7770-456d-9e89-2ec5dd405fa8，禁止落入未挂载根盘。
- 数据库仍为现有 `data/drama_material_jobs.sqlite3`，仅加表和字段，保留所有旧账本。
- API加载 `deploy/youtube-auto-publish-api.conf` 独立drop-in；新worker使用 `deploy/youtube-auto-publish-worker.service`。沿用现有OAuth、Feishu、HK executor配置，不复制密钥进仓库。

## 顺序
1. 比较部署文件live摘要与已记录基线，发现漂移立即停止。
2. 备份精确源文件、静态文件、systemd/drop-in和新配置存在状态；SQLite online backup，旧worker无进行中的上传/首评时才短暂停止。
3. 下载GitHub归档并校验SHA256；stage编译及live feature guard；安装精确文件。
4. 统一账本writer更新两个validator模块并重启，保持已有表/数据/权限。主API与旧YouTube worker刷新代码，防止旧claim处理新workflow。
5. 安装配置空SQL与私有目录；重启API确认健康后启用新worker。不要重启HK媒体服务或其他后台worker。
6. 验证新路由401/权限保护、主健康、公开静态摘要、登录后的SQL未配置零素材、后台任务零新增、systemd稳定。

## 回滚
停止并禁用新worker；恢复本次变更的API/旧publisher/writer/静态文件和drop-in；重启受影响三个既有服务。保留新的SQLite表、审核图片、notification与发布账本；不能用旧DB备份覆盖已发生的发布状态。若有已上传video/session，先记录并人工核对，不清空或重新提交。SQL空态上线没有产生实际发布。部署执行证据将在本文件追加。
