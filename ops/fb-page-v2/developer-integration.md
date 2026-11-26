# Page 画像 V2：开发电脑本地联调

开发电脑可从 Postman、cURL 或本地后端调用：

```text
POST https://ai.yingliangads.com/api/integrations/v2/drama-page-assignments
GET  https://ai.yingliangads.com/api/integrations/v2/drama-page-assignments/healthz
```

两个地址都需要 `Authorization: Bearer <现有 V1 画像接口 Token>`。
POST 使用 UTF-8 JSON。凭据从现有发布服务密钥配置取得，不放进文档或源码。
无需连接香港内网，也无需登记开发电脑的公网 IP。

正式发布继续使用
`http://10.89.0.125:8944/api/integrations/v2/drama-page-assignments`。
HTTPS 入口将请求转发到同一个香港 V2 实例，计算、Page 池授权、回执和幂等规则一致。
本地联调只需切换 URL；Token、请求 JSON 和响应格式不变。
建议 HTTPS 单次总超时 55 秒，连接超时 5 秒；内网仍可使用 50 秒总超时。

## 可直接复制的样例

下面使用已经保存的真实回执。原样重放可检查连通、鉴权和幂等，不会创建广告。
自行测试新请求时，必须使用实际已提交的发布队列，先保存子批参数。
新请求会进入真实 V2 选页账本，不支持随意编造队列 ID 或更换同一子批的参数。

保存为 `request.json`：

```json
{
  "external_task_id": "cross:3053734:smoke-20261126-hk-live",
  "page_group_id": 62,
  "drama_ids": ["DjeK3YIta1", "3H5QDVSe4u", "d1V8LaYBUI"]
}
```

cURL（macOS/Linux；Windows 使用 curl.exe 并按所在 Shell 调整换行）：

```bash
curl --silent --show-error --include --connect-timeout 5 --max-time 55 \
  'https://ai.yingliangads.com/api/integrations/v2/drama-page-assignments' \
  -H "Authorization: Bearer ${FB_PAGE_ASSIGNMENT_API_TOKEN}" \
  -H 'Content-Type: application/json' --data-binary @request.json
```

Windows PowerShell（同目录保存上述 JSON；环境变量由密钥配置提供）：

```powershell
$headers = @{ Authorization = "Bearer $env:FB_PAGE_ASSIGNMENT_API_TOKEN" }
Invoke-RestMethod -Method Post `
  -Uri 'https://ai.yingliangads.com/api/integrations/v2/drama-page-assignments' `
  -Headers $headers -ContentType 'application/json; charset=utf-8' `
  -InFile '.\request.json' -TimeoutSec 55
```

Postman：选择 POST，填写 HTTPS 地址；Authorization 选择 Bearer Token；
Body 选择 raw / JSON，粘贴上述 JSON 后发送。健康检查使用 GET 和同一 Bearer，
正常返回 `{"ok":true,"version":2}`，表示网络和 HTTP 服务可达。

## 调试入口的额外返回

- `401 unauthorized`：入口凭据缺失或错误，未转发到香港服务。
- `426 https_required`：请使用 HTTPS。
- `429 dev_gateway_busy`：调试入口最多 5 个并发；每个源 IP 平均 5 次/秒、突发缓冲 10 次。
  读取 `Retry-After`，至少等待 1 秒后按原键、原池、原列表重试。该限制仅作用于 HTTPS 调试入口。
- `503 dev_gateway_unavailable`：入口到香港服务的连接暂时不可用，按原请求有限重试。
- `413 body_too_large`：请求体超过 65,536 字节。

香港服务的其他状态码、正文、`task_id`、`X-Request-ID` 和 `Retry-After` 原样透传。
网关不会自动重发请求。客户端连接中断或超时仍可能已有已保存回执，必须同键重试。
完整字段、Page 空值兜底和异常事件上报规则见 [正式协议](publisher-integration.md)。

## 运维与回滚

CPU Nginx 终止 HTTPS并校验 Bearer，经过仅监听 CPU `127.0.0.1:19845` 的加密隧道
连接香港 `10.89.0.125:8944`。专用 SSH key 只允许该转发目的地，禁止远程命令、PTY、
agent/X11 转发；已有发布机清单和香港 8944 云安全组不变。

`dev-gateway.py prepare` 在 CPU 生成本机私有 key 并核对已知香港主机指纹；
HK 用 `install-key --public-key-file FILE --backup DIR` 安装受限公钥。
CPU 从 GitHub 的已验证提交部署后执行 `apply --backup DIR`，仅启动独立隧道并 reload Nginx。
实际 Token 校验片段是服务器私有配置，不进入 Git。

CPU 回滚：`python3 RELEASE/ops/fb-page-v2/dev-gateway.py rollback --backup DIR`。
HK 如需撤销本次专用公钥：
`python3 RELEASE/ops/fb-page-v2/dev-gateway.py remove-key --public-key-file FILE --backup DIR`。
回滚只恢复本次文件，保留新增配置或并行改动；不切换选页代码，不恢复业务账本。
