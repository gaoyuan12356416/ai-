# FB Page 发布记录时间显示修复

2026-09-04：发布服务正常，页面却只显示 `created_at_utc`，标题仅为“时间”。提前生成的批次与 UTC 日期共同导致运营误以为自动发布停在前一天。

页面现分别展示计划发布时间、批次创建时间、完成时间，明确固定为 Asia/Shanghai；详情先展示北京时间，再保留明确标注 UTC 的原始审计数据。缺失时间展示破折号，异常时间不伪造。完成时间表示批次终止，是否发布成功仍以对账状态为准。

## 范围和验证

仅修改 static/fb-auto-publish-runs.html，不改后端、调度、队列、Token 或发布账本，不发测试帖，不重启服务。验证包含内联 JavaScript 语法、跨 UTC 日期的时间格式与空值、列表和详情渲染、部署文件 SHA-256 和公开页面读取。

## 部署与回滚

GitHub 精确提交先行。部署前备份 /usr/share/nginx/html/fb-auto-publish-runs.html 与 /root/drama_material_service/static/fb-auto-publish-runs.html，验证这两份与预期基线一致，使用同目录临时文件原子替换。备份置于已验证的数据盘 /mnt/data-disk/fb-auto-post-deploy/backups/。

回滚仅将备份的两份 HTML 原子恢复，不恢复 SQLite，不切换运行中的发布 release。
