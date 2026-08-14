# 部署文档

## 变更内容

发布 23 份 locale HTML、新 hashed JS、46,114-byte hashed WebP，以及包含图片精确 immutable location 的 TT Nginx 配置。

## 配置项

- 新图片 URL：`/tt-drama-code-assets/tt-code-location-guide.0b42fbc64ab4.webp`
- 新 JS URL：由 `scripts/build_tt_drama_code_assets.js` 生成并写入 locale HTML。
- 无环境变量、数据库或服务单元变化。

## 数据库变更

无。

## 部署步骤

1. GitHub 合并/推送后，在服务器 checkout 精确 commit 到 `/mnt/data-disk/tt-code-performance/releases/<commit>`。
2. 确认 Featured 资产脚本 SHA 与现网 `42f490...759b` 一致。
3. 创建 `/mnt/data-disk/tt-code-performance/backups/<CST>-pre-guide-6af3939/`，备份 current 指针、23 locale、三份 TT Nginx 配置和指纹清单，并执行 `sha256sum -c`。
4. 先原子发布 hashed WebP 和 hashed JS到 `/usr/share/nginx/html/tt-drama-code-assets/`，保留旧 hashed 资产。
5. 原子替换 `/etc/nginx/default.d/tt-drama-code-search.conf`，执行 `nginx -t`，仅 reload Nginx。
6. WebP 200 后，逐文件原子替换 23 份 locale HTML。
7. 验收通过后，原子切换 `/mnt/data-disk/tt-code-performance/current` 到新 release。

## 验证步骤

- 23 locale 的缩略图和完整图均引用相同 guide URL及相同新 JS hash。
- `/tt`、`/tt-code` 为200/no-store；`/tt/` 为404。
- WebP SHA与源码一致，200、`image/webp`、immutable。
- 合法code、Content ID、无效code、Featured点击回归通过。
- 390×844、桌面、阿拉伯语无溢出；console无新增错误。
- Nginx active、master PID不变、`NRestarts=0`；API/Redis/TT/Featured未重启。

## 回滚方案

1. 从本次 pre-guide 备份原子恢复 23 locale 和 `tt-drama-code-search.conf`。
2. 将 current 原子指回备份记录的旧 release。
3. `nginx -t` 通过后仅 reload Nginx。
4. 复验 `/tt`、`/tt-code`、旧 JS和 Search/Featured。
5. 新 hashed WebP/JS和新 release保留审计，不做递归删除。

## 注意事项

- 仅切 current 不会更新 Nginx docroot；current 与 live 文件必须成对处理。
- current 同时被 Featured asset service 读取，未评审脚本不得变化。
- 不重启 API、Redis、TT 发布或 Featured 服务，不触发任何真实发布任务。

## 生产记录

- 发布提交：`b0775bc5cbaac53d47529ac366b05ed744fe5731`
- 活动 release：`/mnt/data-disk/tt-code-performance/releases/b0775bc5cbaac53d47529ac366b05ed744fe5731`
- 回滚包：`/mnt/data-disk/tt-code-performance/backups/20260814T182647+0800-pre-guide-6af3939`
- 新 JS：`tt-drama-code-search.45ea9a9af6ac.js`，SHA-256 `45ea9a9af6aceeebfa1efcab7a65f38ff3defc17803291ba09c67afea52c6d8c`
- 新 WebP：`tt-code-location-guide.0b42fbc64ab4.webp`，SHA-256 `0b42fbc64ab49e1c58a6f478a8c8c8f64c90427ce5ef78d06f8b0b145433b2c0`
- Nginx仅reload；master PID `2164`不变，`NRestarts=0`。
- 首次自动门禁因reload后立即命中旧worker响应头而触发完整回滚；加入最多5秒短轮询后重试成功。回滚包两次校验均通过。
