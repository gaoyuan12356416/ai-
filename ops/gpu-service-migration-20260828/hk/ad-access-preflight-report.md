# 广告生成与视觉分析：香港上游访问预检

日期：2026-08-28。批次：gpu-service-migration-20260828T1502。

执行版本：de54ca2a4577d3edb05d47aae583bb7f5c464504，协调者审核、推送并明确授权后执行。

## 结论

香港直连模型目录预检未通过；广告生成和视觉分析暂不切生产。美国服务仍保留，香港仅完成隔离运行时、源码和历史数据准备。尚未迁入正式auth、建立current或启动新的广告生产服务。正式素材生成未验证，不能将离线运行时、历史文件或health测试成功视为上游可用。

| 主机 | HTTP状态 | 安全错误码 | gpt-5.5目录可见 |
| --- | --- | --- | --- |
| 美国 43.166.178.132 | 200 | 无 | 是 |
| 香港 43.154.250.89 | 403 | non_json_response | 未确认 |

每端仅发送一次GET，US作为先行对照。请求未运行Codex进程，没有OAuth刷新、登录、生成或发布，没有代理或重定向。US成功证明本次最小鉴权片段和请求在US可用；不证明HK被拒绝的具体原因。

## 证据与凭据清理

两个主机各自保留以下私有目录中的安全报告：

    /data/migrations/gpu-service-migration-20260828T1502/hk-access-probe/de54ca2a4577d3edb05d47aae583bb7f5c464504/

美国报告为result-US.json，香港报告为result-HK.json。目录权限0700、报告0600；仅保留HTTP状态、安全错误码、目标模型可见性三个字段。

本地程序finally移除两端access-fragment.json后，使用新的只读SFTP连接独立复核：两端片段均不存在。HK的/data/ad-material/auth-source/auth.json与/data/ad-material/current均不存在。凭据仅提取access_token/account_id，未传refresh_token/id_token；没有正式auth-source或CODEX_HOME写入，也未复制私钥。

## 403分类的证据限制

首轮脚本未持久化响应headers、content-type、页面title或body，执行进程已退出。当前只能确定“HK返回非JSON的HTTP403”，不能区分地区政策、账号授权、WAF挑战或其他服务端拒绝，也不能推断同一请求经实际Codex客户端一定会成功。不得用缺失的诊断字段补写原因。

## 增强元数据预检结果

协调者审核推送fd664cf2914e1aa1f3eb9107809df6d5f9b3f42b后，按授权每端各执行一次GET。US仍为200，json、server类别cloudflare、gpt-5.5可见；HK仍为403，html、server类别cloudflare、cf_mitigated_challenge=false、page_category=unclassified_html、safe_error_code=non_json_response。没有保存原始headers/title/body。缺少challenge标记不能证明不存在WAF；现有分类仍不足以确定地区或账号原因。

新结果保存于相同hk-access-probe根下的新SHA目录，旧结果保留。两端新旧SHA目录的access-fragment.json均经独立只读SFTP复核不存在；新目录0700、报告0600。HK正式auth/current仍不存在。此后停止重复原GET，不加代理、不复制缓存、不刷新授权。

## 当前授权归属风险与后续诊断边界

美国必须保留的交互Codex进程组470049/470056和3631458/3631490，与广告共享/root/.codex；vision还会从它复制auth到job私有home，未发现API key覆盖。因此停广告/视觉并不等于冻结managed auth，保留中的交互会话仍可能刷新它。不得停止这些用户会话或复制共享managed auth到香港。正式生产接管仍未授权，需要独立HK登录会话或用户明确批准的隔离方案。

官方[App Server外部token协议](https://learn.chatgpt.com/docs/app-server#3c-log-in-with-externally-managed-chatgpt-tokens-chatgptauthtokens)提供实验性的chatgptAuthTokens模式：宿主传accessToken/account ID，401时由RPC向宿主请求刷新。0.147.0实际原生二进制静态检查发现对应模式、字段及刷新RPC；它不等于Enterprise PAT，也不能把现有OAuth access token改当PAT。此时尚未运行原生客户端。

协调者随后只授权准备独立临时HOME/CODEX_HOME中的原生只读诊断，不代表允许直接运行。合同调查发现0.147.0固定SQLite日志层不受RUST_LOG控制，模型解码错误会记录完整响应正文；未找到标准关闭开关。因此无法满足本次不保留原始正文的约束，不执行原生预检。只完成的RPC驱动草稿已移除，没有复制新凭据或启动Codex。详细精确版本证据见 [原生预检约束报告](native-preflight-contract.md)。

若后续另获批准采用安全可行的客户端，仍只能使用external tokens、拒绝刷新请求，不发thread/turn、不生成、不managed login、不代理、不复制旧cache；还须用本次新生远端cache等证据区分内置模型回退。目录成功依然不能替代生成验收。

以上诊断不需启停HK现有FB、剧集或X，不需改变它们的运行时、驱动、环境或网络设置。实际客户端执行和素材生成仍不可从已完成的GET授权中推定。

## 已完成但不构成上游验收的检查

- 私有Node22.22.2、Codex0.147.0、Python3.9和Pillow8.4.0离线运行时检查。
- 原广告业务源码哈希、历史数据文件与/files读取契约检查。
- 新unit语法、数据目录隔离和登录shell运行时解析检查。
- 首轮本地HK迁移测试14/14通过，增强元数据版本17/17通过；首轮实际probe通过Git精确版本校验。

以上检查不改变本报告的阻塞结论。重新批准广告生产切换前，必须取得有效的上游访问证据并由协调者处理美国停服、最终数据对账与正式授权接管窗口。

## 关联X迁移的验证与回滚边界

已有X离线JSON仅证明直接FFmpeg烟测，未证明原业务repair处理器整链；另行准备的纯离线processor验收须独立批准运行。X实际按7c54切换时尚无新版tunnel依赖baseline。将来回滚须使用现有手动恢复证据和原始隧道状态，由协调者明确恢复原本active的tunnel，再验证CPU18820，不能假定新版自动恢复适用于首版记录。详见README。
