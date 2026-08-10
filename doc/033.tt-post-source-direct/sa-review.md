# SA 需求与方案评审

## 结论

通过。采用独立 profile 的原字节镜像方案，能够满足“不制作直接发”和未来无损回切，同时不破坏历史账本与发布安全门禁。

## 问题清单

| 编号 | 级别 | 问题 | 决策 | 状态 |
| --- | --- | --- | --- | --- |
| SA-001 | P0 | 原始 COS origin 不是当前已验证 URL Property，不能直接交给 TikTok | 原片字节不变地镜像到 `socialkit-cdn.yingliang.tech`，发布仍逐次核对 origin | 已采纳 |
| SA-002 | P0 | 仅改全局开关会让切回时影响在途任务 | 使用独立 `tt-post-source-direct-v1` 冻结任务身份，调度精确匹配 profile | 已采纳 |
| SA-003 | P0 | “不制作”不能绕过媒体和 Creator Info 校验 | 保留下载、ffprobe、SHA、大小、时长、门禁、幂等与核对；只移除 FFmpeg 制作 | 已采纳 |
| SA-004 | P1 | 切换新 profile 后旧 ready 池可能被误迁移 | 不迁移、不改写，旧池保持可回切 | 已采纳 |
| SA-005 | P1 | 源素材规格漂移会把不兼容文件交给 TikTok | 采用最近真实素材验证过的窄合同，不符合时 fail-closed | 已采纳 |

## 决策记录

- 不采用“直接替换域名”：同路径在已验证域名返回 404，不能证明同一对象。
- 不新增 TikTok `FILE_UPLOAD` 实现，继续复用已经审计的 `PULL_FROM_URL` 路径。
- 不关闭 prepare runner；该 runner 在新模式中只做校验和原字节镜像。
- 生产验收不主动触发真实帖子，由用户手动测试。

## PM 修订确认

上述决策已写入 `requirements.md`、测试用例与部署回滚方案。
