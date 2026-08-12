# SA 代码评审

## 结论

通过，可进入 GitHub-first 部署。实现保持历史版本兼容，并将两种视频制作方式隔离到固定服务端路由。

## 评审范围

- 模板输入校验、版本存储与 API 输出。
- prepare / creator-info / publish / reconcile 的冻结版本路由。
- 共享 GPU client 的 loopback 端口边界。
- 编辑页回填、摘要、请求体及 systemd/env 部署合同。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 | P0 | `publisher.py` | 若只在 prepare 选路由，publish/reconcile 会访问错误 worker 账本 | 三阶段每次按冻结 `template_id + version` 解析同一路由 | 已解决 |
| CR-02 | P1 | `publisher.py` | 内部 route map 若允许任意 profile/trim，错误部署可绕过枚举合同 | 显式 route 必须精确匹配两组 profile/trim | 已解决 |
| CR-03 | P1 | `tt_posts/service.py` | 共享 GPU client 默认允许新端口会扩大旧流程网络边界 | 默认仍只允许 18830，direct client 显式单端口 opt-in 18834 | 已解决 |
| CR-04 | P1 | `deploy/` | GPU 8831 已被媒体 origin 配置保留 | direct worker 改用 8832，独立 work root 与 18834 隧道 | 已解决 |

## 编译 / 验证结果

- `py_compile`、`node --check`、`git diff --check` 通过。
- TT auto/app/UI 132 项、GPU worker 73 项、TT posts 141 项，共 346 项通过，0 失败。
- 未发现未解决 P0/P1 问题；未调用真实 TikTok publish。
