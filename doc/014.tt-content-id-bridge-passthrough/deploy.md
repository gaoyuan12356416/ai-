# 部署文档

## 变更内容

- 新增公开移动端中间页 `tt-drama-search.html`。
- 新增参数拼接脚本 `tt-drama-search.js`。
- 新增 Nginx 精确短路径 `/tt`。

## 配置项

无需环境变量。Nginx 配置源文件为 `deploy/nginx/tt-drama-search.conf`，生产目标为 `/etc/nginx/default.d/tt-drama-search.conf`。

## 数据库变更

无。

## 部署步骤

1. 从已推送的 GitHub 精确提交创建只读发布目录并核对提交 SHA。
2. 备份或记录以下生产目标的原状态：
   - `/root/drama_material_service/static/tt-drama-search.html`
   - `/root/drama_material_service/static/tt-drama-search.js`
   - `/usr/share/nginx/html/tt-drama-search.html`
   - `/usr/share/nginx/html/tt-drama-search.js`
   - `/etc/nginx/default.d/tt-drama-search.conf`
3. 只安装上述新增文件，不同步整个 `static/` 目录。
4. 执行 `nginx -t`；仅在成功后执行 `systemctl reload nginx`。

## 验证步骤

1. `curl -I 'https://ai.yingliangads.com/tt?af_adset_id=XXX'` 返回 200、`text/html`、无 Location。
2. `curl -I 'https://ai.yingliangads.com/tt-drama-search.js'` 返回 200、JavaScript 类型。
3. Playwright 390×844 输入 `l9rP6ey2CB`，目标 URL 与验收示例一致。
4. 用覆盖参数尝试验证 `af_dp/c/af_c_id` 仍为固定值。
5. 检查 Nginx 与 `drama-material-api.service` 健康。

## 回滚方案

若这些目标在部署前不存在，则删除五个新增生产文件，执行 `nginx -t` 后 reload Nginx。若任一目标原本存在，则从本次备份目录恢复后再校验和 reload。主 API 无需重启。

## 注意事项

- Nginx access log 会记录入口查询串，不得传 token、手机号等敏感信息。
- W2A 对非法 `content_id` 可能仍返回 HTTP 200，本页不据此判断剧集有效性。
- 生产提交 SHA、备份路径与最终校验结果在发布后补录。
