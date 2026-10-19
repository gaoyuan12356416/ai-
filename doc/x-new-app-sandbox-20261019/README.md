# X 新 App 本机隔离测试

本工具在 `http://127.0.0.1:18880/` 复用现有 OAuth 2.0 PKCE 账号授权、身份验证和刷新实现。源基线为线上 X release `34219a5b621bc7ea095a9f52004ec7286ecc36ec`；OAuth 源文件换行归一化 SHA256 为 `faa18209999f3f3f345db125f71c3c57c821e3c6043251f3cd38dd509dfbaaab`，已与服务器只读核对。

## 启动

```powershell
python scripts/x_new_app_sandbox.py --data-dir D:/codex/sandbox-data/x-new-app-20261019
```

在另一个终端启动专用可交互浏览器（`--playwright-root` 使用 workspace dependencies 返回的 Node modules 目录）：

```powershell
node scripts/x_new_app_sandbox_browser.cjs --data-dir D:/codex/sandbox-data/x-new-app-20261019 --playwright-root <node_modules绝对目录> --self-test
```

该数据目录必须位于仓库之外，名称包含 `sandbox`，通过 Windows ACL 仅允许当前用户和 SYSTEM 访问。凭证保存在 `credentials.json`，账号库和令牌位于 `state/`，不提交 GitHub。启动前清空子进程继承的全部 `X_*` 变量，再指定测试路径；不读取线上 `.env`，不启动 sidecar 的 `serve()`、发布 worker、调度器、短链服务或任何生产服务。

## 新 App 设置与操作

1. User authentication settings 启用 OAuth 2.0，Read and write，Web App / Automated App / Bot。
2. 按用户决定，Callback URL 完整填写 `https://ai.yingliangads.com/x-oauth/callback`；Website URL 可填 `https://ai.yingliangads.com`。不要求额外登记 localhost 回调。
3. 在 Keys and tokens 的 OAuth 2.0 区域获取 Client ID / Client Secret；在本机测试页填写。Consumer Key / Secret 与应用 Bearer Token 不能替代 OAuth 2.0 这两项。
4. 只在专用浏览器点击登录，在 X 官方页面完成一个账号的授权。浏览器拦截测试授权生成的准确 state 对应的正式域名回调，将原始参数交给本机隔离服务，收到本机结果后再跳回本机页面。兑换令牌时 `redirect_uri` 仍为正式回调，与新 App 注册值一致。
5. 点击账号验证，检查身份、实际 scopes、refreshability、会员状态。应用 Bearer 检查仅做一次公开用户读取，可能计入新 App 用量。

配置接口及验证接口要求本机 Host、会话 Cookie、准确 Origin、JSON 和 CSRF Header。创建授权和接收回调还必须携带仅专用浏览器持有的回调保护凭证。浏览器对正式 AI 域名的其他请求全部在本机拒绝；准确匹配的回调也不会发送到生产服务器。所有生产 `/internal/*` 和发布路由均不可访问。测试账号默认 `publish_approved=0`；数据库只创建授权表，不创建发布表。

测试通过仅说明授权和已执行的读取可用；真实发帖、媒体上传、令牌刷新需要各自验证后才能判定通过。不自动替换线上 App，不复制或导入生产账号 Token，不以登录成功推断新 App 余额或发布能力。

## 当前验证记录（2026-10-19）

- 新 App Bearer Token 真实请求 `GET /2/users/by/username/XDevelopers` 返回 HTTP 200。
- 7 项本机测试通过：环境变量与存储隔离、来源/会话/CSRF 检查、密钥不回显、生产发布路由关闭、复用原 OAuth 代码的模拟完整授权与验证、单账号边界、无效回调拒绝、普通浏览器禁止授权。测试使用假凭证且不访问 X。
- 专用浏览器自检通过：回调保护有效，未匹配 state 在本机阻断，匹配测试 state 转交本机，假的授权码由本机拒绝；没有真实账号授权或发帖。
- 页面已渲染并检查截图；初始账号数 0、OAuth 2.0 配置待填写、发布入口关闭。
- 真实账号授权、身份与权限验证仍等待用户完成登录，不标记通过。

## 停止与回退

关闭本机测试进程即可停止。保留测试数据供核对。该过程不部署服务器，不修改线上配置、账号、账本或定时任务，不需要生产回滚。

官方参考：[OAuth 2.0 PKCE](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code)。
