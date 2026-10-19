# CPU 独立测试环境

用户最终决定在 CPU 服务器建立独立环境，不再使用本机浏览器拦截或扩展。

- 入口：`https://ai.yingliangads.com/x-app-test/`，使用默认浏览器及已有 AI 后台管理员登录。
- 服务：`x-new-app-test.service`，新建独立系统用户 `x-app-sandbox`，只监听 `127.0.0.1:18881`。
- 根目录：`/mnt/data-disk/x-new-app-sandbox-20261019`；不可复用生产数据库、Token 或旧 App 配置。
- `data/credentials.json` 只保存本次新 App 配置，由服务用户读取，目录 0700、文件 0600。`data/state/` 独立保存 OAuth 状态、账号库和 Token。
- 源码按本分支的确切 GitHub commit clone 到 `releases/<commit>`，`current` 指向该版本。
- 服务仅提供一个账号的 OAuth 登录、身份/权限读取和应用只读检查，不启动任何发布 worker、调度器或生产依赖。

## 正式 callback 的隔离分流

继续登记 `https://ai.yingliangads.com/x-oauth/callback`。新测试服务给 state 加上 `xtest_20261019_` 前缀，仍以完整 state 的哈希保存到独立测试数据库。

Nginx 的 HTTP 级 map 仅把符合 `^xtest_20261019_[A-Za-z0-9_-]{43}$` 的回调转到新端口；普通 state 仍转到原 `127.0.0.1:8810/callback`。令牌兑换始终发送原正式 callback URI。callback 路由禁止访问日志，测试入口同样不记录请求日志。

不修改旧 App 凭证、线上账号库、线上服务文件或服务进程。公开入口会增加测试 location 和一条 state 分流，因此必须备份 Nginx 原配置、`nginx -t` 后平滑 reload。保留正常回调原去向、健康接口及其他 location。

## 权限与操作

测试首页通过 loopback 只读调用现有 `/api/auth/status` 校验 Feishu 管理员身份，不导入主应用或复制主应用密钥。未登录跳转现有 Feishu 登录，回到本页。测试 API 另要求独立 Secure/HttpOnly/SameSite Cookie、同源 JSON 和 CSRF Header。callback 也校验管理员与测试 Cookie。

打开测试入口 → 登录 X 测试账号 → 完成官方授权 → 回到测试页 → 验证账号与权限。已保存的新 App Client ID / Secret 无需重新输入。真实发帖、媒体上传及生产替换另行验收，不能从登录或只读通过推断。

## 验证与回退

本地 `python scripts/test_x_new_app_cpu_sandbox.py` 验证登录门禁、来源/会话/CSRF、发布路由关闭、state 前缀完整往返、正式 redirect URI、独立 Token 和单账号限制。

服务器检查新服务 health、管理员门禁、普通/测试 callback 去向、独立数据库 `quick_check` 和发布表数；核对原主服务及 X 服务 PID 未变、原 health 正常。

回退：恢复部署前 `/etc/nginx/default.d/x-oauth.conf`，移走新 map/location 配置，`nginx -t && systemctl reload nginx`，停止 `x-new-app-test.service`。保留测试数据和日志供核对，不删除或回退生产账本。
