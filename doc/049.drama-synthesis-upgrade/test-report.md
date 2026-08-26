# 测试报告

## 结论

提交 `25b8af9` 的独立 QA 代码结论为 **PASS**，未发现候选 P0/P1；BUG-001 至 BUG-005 已关闭。整体 production release 仍为 **HOLD**：短链 writer/owner 与既有数字 ID 命名空间冻结尚未落实。生产来源 hostname 已由 CPU SQLite 只读证据精确确认。未调用真实 YouTube API，未创建视频/评论，未部署。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: | ---: |
| Offline focused unittest | 29 | 29 | 0 | 0 |
| Python compile targets | 6 | 6 | 0 | 0 |
| App import/zero-output integration check | 1 | 1 | 0 | 0 |
| Inline JavaScript syntax blocks | 4 | 4 | 0 | 0 |
| 独立浏览器合同 | 8 | 8 | 0 | 0 |
| 独立 targeted bug 回归 | 8 | 8 | 0 | 0 |
| 独立 identity 对抗 | 7 | 7 | 0 | 0 |
| 独立 stale-write fencing | 5 | 5 | 0 | 0 |
| 独立 migration concurrency iterations | 400 | 400 | 0 | 0 |
| 最终 broad regression（仅执行一次） | 1,894 collected | 1,885 | 5（baseline/unrelated） | 3 skip + 1 baseline collection error |

## 覆盖证据

- 自动/手动 recipe、冻结/冲突、GPU identity。
- 精确短链目标、wrapper、幂等、无 publisher 失败关闭。
- YouTube channel eligibility（含 identity-read scope）、刷新后/外部写入前频道身份核验、comment scope、operation idempotency、generation fencing、续租 heartbeat、视频/评论分态、session query retry、unknown fail closed、duplicate confirmation。
- UI 四项默认未选、零输出 backend guard、新 payload 删除字段。
- `app.py`、核心模块、worker 和测试脚本 compile；两个静态入口共 4 个内联 JS 块语法通过。
- 独立 QA：浏览器 8 PASS；targeted bugs 8/8；identity 7/7；stale writes 5/5；migration concurrency 400/400。
- 最终 broad regression 一次收集 1,894：1,885 PASS、3 SKIP、5 FAIL、1 collection ERROR。全部 6 个非跳过 non-pass 均复现于 base，证明为 baseline/unrelated；对应文件在 `6f8bdf0..25b8af9` 未变化，不归因于候选。

命令：

```text
python scripts/test_drama_synthesis_upgrade.py
python -m py_compile app.py features/drama_synthesis/core.py features/drama_synthesis/gpu.py features/drama_synthesis/youtube.py scripts/drama_youtube_publish_worker.py scripts/test_drama_synthesis_upgrade.py
```

## 缺陷

独立评审/QA 共发现 5 个候选缺陷：BUG-001（live schema 列名）、BUG-002（random-template YouTube source legacy-row 路径）、BUG-003（known-safe 评论重试被跳过）、BUG-004（刷新 token 未绑定冻结频道）与 BUG-005（lease 缺少 generation fencing）。五项均已修复并通过独立定向回归，状态关闭。十进制 ID 预校验和 quote/backslash 对抗输入也已验证在 SQL 构造前失败关闭。

## 遗留风险

- 浏览器、404 expired、频道身份核验、generation fencing、lease crash recovery 和 migration concurrency 已通过独立 QA。
- HK 真机 asset/render/tunnel 是部署 gate，仍须在切流前按部署文档演练。
- 生产 source allowlist 已通过 CPU SQLite 只读样本确认：`advertising-1306474899.cos.ap-hongkong.myqcloud.com` 与 `ai.yingliangads.com`，禁止通配符。
- CloudFront/S3 短链 writer/owner 和数字 ID 命名空间冻结仍是唯一已知外部 release blocker；未配置时功能按设计失败关闭。

## 发布建议

代码可进入条件部署准备，但 production release 仍为 HOLD。短链外部 blocker 与全部部署 gate 关闭后，GitHub-first production deployment 已获根授权。真实 YouTube publish/comment 不包含在部署授权内，必须另行获得精确授权。
