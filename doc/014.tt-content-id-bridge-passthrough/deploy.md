# 部署文档

## 变更内容

- 新增公开移动端中间页 `tt-drama-search.html`。
- 新增参数拼接脚本 `tt-drama-search.js`。
- 新增 Nginx 精确短路径 `/tt`。
- 2026-07-31 将固定 W2A 跳转目标从 `/ads/0/2049/view` 切换为 `/ads/101/2250/view`，参数规则不变。

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
5. reload 后轮询 `/tt` 至返回 200，再判断发布结果；不要在旧 worker 切换完成前立即失败回滚。

2026-07-24 首次发布：

- GitHub 分支：`codex/tt-content-id-bridge-passthrough-20260724`
- 生产提交：`4328ac02024e19ba661926ee4beb4490eb5a576f`
- 发布目录：`/root/releases/ai-tt-bridge-4328ac02024e`
- 回滚点：`/root/backups/drama_material_service/20260724T101647Z-tt-bridge-4328ac02024e-retry`
- 五个目标在发布前均不存在，原始状态记录见该回滚点的 `original-state.tsv`。
- 执行 `nginx -t` 成功后仅 reload Nginx；`drama-material-api.service` 未重启。

## 验证步骤

1. `curl -I 'https://ai.yingliangads.com/tt?af_adset_id=XXX'` 返回 200、`text/html`、无 Location。
2. `curl -I 'https://ai.yingliangads.com/tt-drama-search.js'` 返回 200、JavaScript 类型。
3. Playwright 390×844 输入 `l9rP6ey2CB`，目标 URL 与验收示例一致。
4. 用覆盖参数尝试验证 `af_dp/c/af_c_id` 仍为固定值。
5. 检查 Nginx 与 `drama-material-api.service` 健康。

2026-07-24 首次发布结果：

- `/tt?af_adset_id=XXX`：HTTP 200、`text/html`、无 Location、`Cache-Control: no-store`。
- `/tt-drama-search.js`：HTTP 200、`application/javascript`。
- 线上 HTML/JS 在应用副本和 Nginx 公开副本之间 SHA-256 一致。
- 390×844 Playwright 公网验证通过，中间页控制台 0 error、0 warning。
- 输入 `l9rP6ey2CB` 后实际点击进入：
  `https://www.dramawavew2a.com/ads/0/2049/view?af_dp=l9rP6ey2CB&c=TTpost&af_c_id=0001&af_adset_id=XXX`
- 入口伪造 `af_dp=evil&c=evil&af_c_id=evil` 时，生成链接仍使用固定核心值。
- 2026-07-27 再次验证：Nginx、`drama-material-api.service` 均为 active，公开状态和文件哈希未漂移。

### 2026-07-31 跳转目标切换

- GitHub 分支：`codex/tt-w2a-2250-20260731`
- 生产代码提交：`85d9e8e3d8e3500e370e16df7dcc46ee5b93487a`
- 发布目录：`/mnt/data-disk/tt-bridge-releases/ai-tt-bridge-85d9e8e3d8e3`
- 回滚点：`/mnt/data-disk/tt-bridge-backups/20260731T165543+0800-85d9e8e3d8e3`
- 仅替换应用静态副本和 Nginx 公开副本中的 `tt-drama-search.js`；未修改 resolver、W2A 资源缓存来源、HTML 或 Nginx 配置。
- 两份线上 JS 的 SHA-256 均由 `a3529f7d72cfd7bb9b4b8ed5eb8b8ffcaf294c8ad66c2fd8d4938774946e35cd` 变为 `635d50a21aa69fcf68f84611e08ac4e9195957476739fcd40a5bf75a957e1a80`。
- `nginx -t`、Nginx reload、`nginx` 与 `drama-material-api.service` 健康检查均通过；主 API 未重启。
- 真实浏览器输入 `l9rP6ey2CB` 并点击后进入：
  `https://www.dramawavew2a.com/ads/101/2250/view?af_dp=l9rP6ey2CB&c=TTpost&af_c_id=0001&af_adset_id=XXX`
- 目标页返回对应剧集，最终地址完整保留 `af_dp`、`c`、`af_c_id` 和 `af_adset_id`。

## 回滚方案

当前版本回滚时，从 2026-07-31 回滚点恢复两份 JS：

```bash
backup=/mnt/data-disk/tt-bridge-backups/20260731T165543+0800-85d9e8e3d8e3
install -m 0644 "$backup/source-tt-drama-search.js" \
  /root/drama_material_service/static/tt-drama-search.js
install -m 0644 "$backup/public-tt-drama-search.js" \
  /usr/share/nginx/html/tt-drama-search.js
nginx -t && systemctl reload nginx
```

恢复后核对两份 JS 的 SHA-256 均为 `a3529f7d72cfd7bb9b4b8ed5eb8b8ffcaf294c8ad66c2fd8d4938774946e35cd`。主 API 无需重启。

## 注意事项

- Nginx access log 会记录入口查询串，不得传 token、手机号等敏感信息。
- W2A 对非法 `content_id` 可能仍返回 HTTP 200，本页不据此判断剧集有效性。
- 首轮发布的即时探测命中了 reload 前的旧 worker 并触发自动回滚；确认根因后改为就绪轮询，第二轮成功。详见 `bugs/BUG-001.md`。
