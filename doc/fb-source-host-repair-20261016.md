# FB 制作来源域名和错误分类修复

当素材来源为 socialkit-cdn.yingliang.tech，GPU 仅允许旧 COS 域名时，prepare 校验在制作和 Meta 发布之前返回 invalid_request/source URL host invalid。旧客户端丢弃具体原因，日报无法区分该错误与平台发帖失败。

客户端只识别该受控上游消息并转为 fb_auto_source_url_not_allowed / 源视频域名不在制作服务白名单；其他 invalid_request 显示视频制作参数校验失败，绝不转发任意上游文本或凭据。

香港 GPU 配置只追加精确业务 CDN，保留 COS、HTTPS、无凭据、无 fragment、禁止下载跳转等现有校验。服务来源配置与代码分开备份，使用最后加载的 EnvironmentFile，重启后读取运行进程配置验证。

生产旧 failed/unknown/账本不改动、不重试。部署前确认历史无可执行 planned/ready，只保留未知结果门禁；未来已冻结计划照常执行。测试使用假传输和纯校验函数，无真实 Meta 发布。

CPU release路径仍标记d2a6e91；实际gpu.py已叠加507c604d192e3e30e5b47348904d54adc3b55ade的手动素材批次支持。本修复以该已核实源码为基础，仅替换 features/fb_auto_posts/gpu.py，保留当前完整运行目录中的其他功能。GPU 不替换源码，避免覆盖已部署的 compositor 和 render-stall 修复。
