# YouTube 素材上传人列表

发布素材弹窗的上传人选项只显示曾登录 AI 后台的用户：读取本地
`drama_admin_user.login_count > 0` 的非空邮箱，通过只读副本
`admin_user_group.email -> sub_user_id` 映射，与素材上传人 ID 取交集。
姓名不参与身份匹配；未知上传人和未匹配记录不显示。
登录历史按现有上传人缓存刷新，点击素材刷新可提前更新。

只调整下拉列表；素材范围、上传人筛选、任务、发布和归因均保持原有语义。
映射失败沿用上传人缓存的错误提示及重试，不回退显示全部用户。

验证：`python -m unittest discover -s tests -p 'test_youtube*.py' -v`，
及 `test_post_product_scope.py` 的 YouTube 素材/上传人 SQL 用例。
上线前使用生产只读适配器核对候选集合，上线后通过现有 Cookie 会话
只读请求 `/api/youtube-auto-publish/materials`，核对上传人和素材结果。

部署：先推送 GitHub 分支 `codex/youtube-uploader-login-filter-20261116`，
CPU fetch 精确提交后在数据盘建立该提交的独立 worktree；运行：

```sh
python3.9 ops/youtube-uploader-login-20261116/deploy.py COMMIT
```

仅安装 `features/youtube_auto_publish/{source,runtime}.py`，备份和 manifest
保存在 `/mnt/data-disk/youtube-uploader-login-20261116/backups/TIMESTAMP`。
仅重启主 API，按 YouTube lane 快照恢复原有依赖调度状态；发布 worker、
统一 writer 和原本暂停的旧 worker 保持原状。无需 GPU 切换或队列排空。

从同一提交的 worktree 回滚：

```sh
python3.9 ops/youtube-uploader-login-20261116/deploy.py COMMIT --rollback BACKUP
```

回滚仅恢复两份代码、重启主 API 并核对依赖，不还原数据库、回执或业务记录。
存在后续代码更新时先核对差异，避免覆盖其他人的变更。
