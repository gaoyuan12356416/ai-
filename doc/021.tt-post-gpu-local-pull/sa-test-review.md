# SA 测试用例评审

## 结论

测试设计有条件通过，可以进入实现与执行阶段。用例已覆盖后端分支、不可变
文件身份、HTTP/Range、路径安全、unknown 禁清理、明确终态宽限清理、COS
回退、发布门禁和基础设施阻塞。

本地自动化已执行：GPU 专项 51/51、TT 相关全量回归 224/224 通过；这些
结果只证明代码和 loopback 合同，不证明生产公网链路或 TikTok 拉取已通过。
DNS、80/443、安全组、证书与 TikTok URL Property 未具备时，TC-038 至
TC-040 必须保持阻塞；不得用 loopback 结果替代公网证据。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| STR-001 | TC-010～TC-017 | 只断言状态码不足以证明 Range 正确 | 每个成功 Range 必须把响应字节与源文件切片逐字节比较，同时断言 Length 和 Content-Range | 核心单段自动化通过；实片抽样待外部条件 |
| STR-002 | TC-011 | HEAD 容易误发 body 或与 GET 头不一致 | 对完整和 Range HEAD 分别检查状态/头相同且 body 为 0 | 完整 HEAD 自动化通过；Range HEAD 补充项待执行 |
| STR-003 | TC-019～TC-021 | 只测 `../` 不能覆盖编码和软链接绕过 | 加入双编码、反斜杠、额外 segment、超长 path、symlink、目录和 FIFO | 已写入用例，待执行 |
| STR-004 | TC-025～TC-031 | 生命周期只测内存状态会漏掉重启恢复 | 所有 unknown/终态/清理用例都从磁盘账本重启恢复，并检查未误删相邻 job | 核心持久账本、终态和身份冲突通过；重启故障注入待执行 |
| STR-005 | TC-026 | unknown 时仅“不清理”还不足以防重发 | 同时断言 publish 重试不再 init，只能按已有 publish_id reconcile 或 needs_review | 自动化通过 |
| STR-006 | TC-028/029 | 一小时边界可能存在 off-by-one | 使用冻结时钟分别验证 3599 秒与 3600 秒，并覆盖时钟倒退 | 已写入主边界；执行时补时钟倒退 |
| STR-007 | TC-024 | 低盘用例可能通过 mock 删除掩盖风险 | 断言下载/转码未开始，pending/unknown 文件 inode 与哈希均未变化 | 已纳入执行要求 |
| STR-008 | TC-003/035/036 | 新 local 改造可能破坏 COS 旧行为 | 复用既有 COS 测试并增加旧 manifest、后端切换和精确 URL 回归 | 自动化通过 |
| STR-009 | TC-032/033 | 只测 GPU 门禁会漏掉 CPU 已消费素材 | 同时检查 pool/run/queue、GPU init mock 和事件账本，门禁关闭不得消费 | TT 全量自动化通过 |
| STR-010 | TC-038/039 | 从 GPU 本机 curl 不能证明公网和代理行为 | 至少从一个非 GPU 网络验证 DNS、可信 TLS、无重定向、首/中/尾 Range；控制路由不可达 | 阻塞待外部条件 |
| STR-011 | TC-040 | DNS 所有权不等于 TikTok URL Property 已验证 | 必须归档 Developer Portal 中与实际 origin/prefix 一致的验证证据 | 阻塞待 App 负责人 |
| STR-012 | TC-041 | 小 fixture 不能证明 34.8 分钟实片和带宽路径 | 用素材 4665764 的真实关闭态成片记录 SHA、大小、ffprobe、外网字节抽样和 COS/TikTok 调用计数 | 已写入用例，待执行 |
| STR-013 | 日志脱敏 | 错误路径可能把签名 URL 打入 journal | 自动扫描 stdout/stderr/journal fixture，禁止密钥、Token、Authorization 和完整 signature URL | 已写入用例，待执行 |
| STR-014 | 回滚 | 只验证服务启动不能证明旧任务安全 | 回滚后分别读取旧 local、旧 COS 和新 COS job，确认账本不改写、文件不误删 | 已写入用例，待执行 |
| STR-015 | TC-043 | 三个布尔门禁测试未覆盖后端切换后的 URL Property 漂移 | 分别验证空值、旧后端 origin、当前后端 origin；前两者必须 `ready=false` 且 init=0 | 自动化通过 |
| STR-016 | TC-044 | 只核对配置 origin 不能证明实际 prepared URL 安全 | 篡改 manifest URL origin，断言逐次核对失败且 init=0 | 自动化通过 |
| STR-017 | TC-026/045 | init HTTP 错误若被误判为确定拒绝会造成误删 | 固定 5xx/408/409/425/429 为 outcome unknown；`init_rejected` 与 unknown 首版均不得自动清理 | 自动化通过 |
| STR-018 | TC-009/021 | 只做路径 lstat 未覆盖检查后替换 symlink 的 TOCTOU | 故障注入在校验与读之间替换路径；no-follow fd/fstat 必须拒绝或继续读取原普通文件 | 执行时必须覆盖 |
| STR-019 | TC-009/023 | 每 Range 全量哈希会制造不可接受的放大 | 断言内容 SHA 发生在固化/reuse/publish，正常并发 Range 不重复全文件哈希 | 执行时必须覆盖 |

## QA 修订确认

2026-07-30 已按 SA 建议补充完整 GET/HEAD/Range、路径绕过、重启恢复、
终态边界、低盘、门禁、COS 回退、公网与真实长片用例。代码自动化与独立
评审已完成；生产关闭态、公网和真实长片证据仍按测试报告保持阻塞。
