# 素材选择倒序与上传人筛选

“选择发布素材”按素材 ID 数值倒序展示，较新的素材排在前面。上传人下拉默认“全部上传人”，可与名称 / ID 搜索组合使用，刷新和预览返回保留条件。

上传人取 `ads_custom_source.user_id`，关联 `admin_users.id`，优先显示 `name`、其次 `username`。缺少映射时显示用户 ID，空 / 0 显示“未知上传人”。不使用设计人或发起人替代上传人。

素材配置继续使用 `product='Drama-社媒专用素材' AND category='dramawave_post'`。新增 `uploader_id`、`uploader_name` 两个 SQL 投影字段。`GET /api/youtube-auto-publish/materials` 新增可选 `uploader_id`，在 SQL 的 `ORDER BY CAST(pool.id AS UNSIGNED) DESC LIMIT 100` 之前过滤；空值表示全部，`0` 表示未知，其他值必须为正整数 ID。

接口新增 `uploaders`，从完整配置范围分组取得，不从当前 100 条视频推导，也不随搜索或所选上传人缩减。选项缓存按 SQL 区分，视频缓存按 SQL + 关键词 + 上传人区分。冷缓存继续后台读取和有界轮询；更换筛选时旧请求不能覆盖新结果。提交素材仍 fresh-check，发布和归因逻辑不变。

验证：

- Python 88 项通过：素材专项 6、现有流程 45、HTTP 权限与路由 18、频道缓存 19。
- 新专项在真实 SQLite 查询引擎上验证数值排序、筛选先于 100 条限制、全量上传人选项、缓存隔离、非法 ID、刷新和提交时重新核验；仅适配 MySQL 的 UTF-8 字面量语法。
- Playwright 本地接口替身完成桌面 1366×900、手机 390×844 的 20 项交互断言，覆盖组合筛选、清空、刷新保留条件、慢响应竞态、DOM 保留、预览返回及确认选择。两种布局截图人工检查通过。替身视频无实际文件，预览触发预期 404；本次验证的是预览弹窗返回状态，不代表真实媒体播放验证。
- JS / Python 语法检查和 `git diff --check` 通过。
- 上线前真实 63350 副本只读查询确认当前配置 46 条，上传人分组为 789（29）、841（16）、0（1）；排序首条 6691347。上线后以实际接口验收为准。

部署使用 `scripts/deploy_youtube_material_picker.py --commit <完整 GitHub SHA>`，先在服务器从 GitHub fetch 精确提交并解包至 `/mnt/data-disk/deploy/youtube-auto-publish/releases/<SHA>`，写入 `.github-verified-commit`。先运行同命令加 `--check`，通过后执行安装。

部署只覆盖两个素材模块、SQL 配置、两份静态 HTML / JS / CSS，并在当前生产 `app.py` 原文中精确替换一行素材路由参数。保留现有停用模块和共享导航版本 `20260915retired`。10 个目标文件备份到数据盘，校验旧 SHA 后原子替换。只重启 `drama-material-api.service`，保留所有发布 worker 状态，不修改数据库、预约、发布账本或消息。

回滚使用同一 GitHub release 的 `scripts/deploy_youtube_material_picker.py --rollback <备份目录>`，只恢复这 10 个文件并重启 API；检测到后续文件变更则拒绝覆盖。数据库和历史任务不参与恢复。

上线提交、备份目录与最终验收将在完成后补入本文件。
