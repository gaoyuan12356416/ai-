# SA 代码评审

## 结论

2026-07-30 最终复核：源码层面无剩余 P0/P1，可在 COS 模式和三门禁关闭的
前提下合并、部署。生产 local 入口仍受外部基础设施与 TikTok 合规条件阻塞。

## 评审范围

- `features/tt_gpu/worker.py`
- `deploy/tt-post-gpu.env.example`
- `deploy/tt-post-gpu-media-nginx.conf.example`
- `scripts/test_tt_gpu_worker.py`
- `doc/021.tt-post-gpu-local-pull/`

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | media handler | 切回 COS 后 handler 跟随当前 backend，旧 local URL 全部 404 | 数据面独立持有 LocalMediaStore | 已修复并回归 |
| CR-002 | P1 | publish gate | 当前 backend 的 verified origin 可能放行冻结为另一 backend 的 URL | 每次 publish 对实际 prepared URL origin 再核对 | 已修复并回归 |
| CR-003 | P1 | cleanup | ledger/manifest/job 身份不足可能误删相邻 job | 删除前核对 ledger job、manifest job、key、SHA、大小 | 已修复并回归 |
| CR-004 | P0 | init outcome | HTTP 5xx/限流被误记确定拒绝后可能自动删源文件 | 5xx/408/409/425/429 归 unknown；init HTTP 结果首版不自动清理 | 已修复并回归 |
| CR-005 | P1 | storage durability | rename/unlink 与账本 fsync 顺序可能导致崩溃后文件/账本漂移 | 文件、job 目录、media root 按顺序 fsync 后再落 manifest/ledger | 已修复并回归 |
| CR-006 | P1 | prepare admission | 并发下载共用同一空闲空间检查，可能打满磁盘 | 新制作串行入场并预留 max source/output 与余量 | 已修复并回归 |
| CR-007 | P1 | media fd | 客户端在响应头阶段断开可能泄漏 fd | 打开后全程 try/finally，始终关闭同一 no-follow fd | 已修复并回归 |
| CR-008 | P1 | ready reuse | 长制作占全局槽时缓存命中也被阻塞 | 槽前复用，入槽后再次检查 | 已修复并回归 |
| CR-009 | P2 | config/monitoring | 非法端口、异常 prefix 和 local 磁盘/清理状态可观测性不足 | 规范化 fail-close；health 暴露无敏感容量与清理状态 | 已修复并回归 |
| CR-010 | P2 | media handler | manifest/文件异常产生 `TTGPUError` 时可能中断连接 | handler 统一 fail-close 为 404，并增加文件大小漂移回归 | 已修复并回归 |

## 编译 / 验证结果

- GPU worker 51/51 通过。
- TT 相关全量回归 224/224 通过。
- Python 语法检查与 `git diff --check` 通过。
- 独立最终复核结论：P0=0、P1=0。
