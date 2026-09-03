# 部署文档

## 变更内容

发布两份静态页面，新增 YouTube 发布记录和状态展示。

## 配置项

无。

## 数据库变更

无。

## 部署步骤

1. 从 GitHub 精确提交检出发布文件。
2. 备份线上两份静态页面并记录 SHA-256。
3. 校验两份新页面一致、内联脚本语法通过。
4. 原子替换 `/root/drama_material_service/static/` 和 `/usr/share/nginx/html/` 中对应文件。
5. 静态变更不重启 API；如 Nginx 配置未变，不 reload。

## 验证步骤

- 公网页面 HTTP 200，两个发布位置的文件哈希一致。
- 浏览器模拟 POST 后详情出现记录与中文状态。
- Worker 保持运行，生产队列不因部署产生新记录。

## 回滚方案

用部署前备份恢复四个静态文件；复核哈希和 HTTP 200。数据库无需回滚。

## 注意事项

生产验收不得为测试创建真实 YouTube 发布。

## 生产记录（2026-09-03）

- 代码提交：`7ac4b55a402c90a590610505d2280f5aec6c6afc`。
- 服务器：`43.166.187.96`。
- 发布路径：`/root/drama_material_service/static/` 与 `/usr/share/nginx/html/`。
- 备份：`/mnt/data-disk/drama-synthesis-cpu/backups/20260903T121700+0800-youtube-detail-status-pre-7ac4b55a402c90a590610505d2280f5aec6c6afc`。
- 发布哈希：`71848c62d166231462210ab1559f26df6e762e5b697af406dec6c9b39b47e9dd`，GitHub release、四个线上文件和公网响应一致。
- 服务：API、YouTube Worker 均为 `active`；未重启。
- 队列：发布前后均为 0，12 点时段没有新增 YouTube POST。

回滚：从上述备份目录分别恢复 `root-static/*.html` 到 `/root/drama_material_service/static/`，恢复 `nginx-html/*.html` 到 `/usr/share/nginx/html/`，再校验备份 `SHA256SUMS` 与公网 HTTP 200；无需恢复数据库或重启服务。
