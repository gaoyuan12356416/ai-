# Codex 0.147.0 原生预检调查：不执行

2026-08-28。仅查官方文档、已安装二进制和官方rust-v0.147.0源码（tree be6e8eac029b183056b7e4402879f15d2c85f61b）。**未执行native客户端，未提取新凭据，未新增模型请求。不可部署的驱动草稿已删除。**

- **协议存在。** 官方app-server支持实验性chatgptAuthTokens外部授权，接收access token/account ID，刷新由宿主RPC提供；它不等于Enterprise PAT。0.147.0的外部凭据使用ephemeral存储，可拒绝刷新，但这不能解决内部日志风险。[官方协议](https://learn.chatgpt.com/docs/app-server#3c-log-in-with-externally-managed-chatgpt-tokens-chatgptauthtokens)、[版本实现](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/login/src/auth/manager.rs#L2813)

- **列表可能回退。** model/list刷新失败后仍可返回内置模型。将来若获批安全可行的客户端，目录验收至少需要全新且无注入cache/catalog的CODEX_HOME、本次新生models_cache.json、client_version=0.147.0、窗口内fetched_at及原始models含gpt-5.5；仅列表出现模型不算成功，也不证明生成能力。[回退](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/models-manager/src/manager.rs#L340)、[远端成功后写cache](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/models-manager/src/manager.rs#L412)

- **日志约束不能保证。** 模型解码错误会包含完整resp.body，刷新错误随后被记录。app-server独立SQLite日志层默认TRACE，RUST_LOG只控制stderr；features.sqlite已Removed，无有效关闭作用。未找到标准关闭开关，因此不运行，不使用RAM/无效目录、改二进制等手段规避约束。[原始body错误路径](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/codex-api/src/endpoint/models.rs#L70)、[独立日志层](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/app-server/src/lib.rs#L647)、[TRACE过滤](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/state/src/log_db.rs#L53)、[已移除开关](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/features/src/lib.rs#L987)

- **不夸大泄露结论。** 正常入口只确认记录method/id，未证实有效登录必然把token落日志；阻断依据是已确认的原始响应错误路径。

- **授权不能靠停广告冻结。** 美国广告/vision与必须保留的交互Codex会话共享/root/.codex，交互会话仍可能刷新它。正式HK接管需要独立登录会话或用户明确批准的隔离方案；不停止用户会话、不复制共享managed auth、不代理绕过两次403。广告迁移保持未完成。
