# 013.x-post-media-repair 部署文档

## 拓扑

```text
CPU daily runner
  -> 127.0.0.1:18820
  -> SSH reverse tunnel
  -> GPU 127.0.0.1:8820 worker
  -> NVENC
  -> immutable COS object
  -> CPU download + SHA/size/probe
  -> frozen queue
```

## 部署顺序

1. 本地全量回归、语法检查和差异检查通过后提交并推送 GitHub。
2. CPU 备份 SQLite、daily env、systemd unit 和当前 release 指针。
3. GPU 备份共享 COS env，收紧为 0600；从精确 GitHub commit 构建独立 release。
4. 在 CPU 生成独立修复 Bearer，不输出内容；GPU 经现有 SSH 信任链安全拉取同一文件。
5. GPU 安装 worker unit 和 tunnel unit，先验证 GPU 本机 health，再验证 CPU 回环 health 和错误 Bearer 403。
6. CPU 从同一精确 commit 构建 release，安装 daily unit，增量加入 repair 配置，切换 symlink 并只重启 X sidecar。
7. 验证 SQLite 新列、计划只读恢复接口、timer 和既有发布记录。
8. 使用显式素材 ID 运行只修复/复检 backfill；先单条 canary，再处理剩余目标。
9. 核对 pool 校验状态、COS/manifest、queue/log/Post 零新增以及服务最终状态。

## 回滚

- 停止并禁用新 worker/tunnel，恢复 GPU 旧配置权限或备份。
- CPU 原子切回旧 release，恢复旧 daily unit/env 后 daemon-reload 并重启 X sidecar。
- SQLite 新列为向后兼容增量字段，旧代码可忽略；不得因回滚删除真实发布日志。
- 已生成的 content-addressed COS 对象和 manifest 保留，避免破坏后续审计与已冻结队列。
- backfill 只清除已通过复检的 pool 错误；如需恢复旧展示，可依据部署前 SQLite 备份逐条核对，不做整库盲目覆盖。
