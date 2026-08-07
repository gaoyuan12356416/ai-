# SA 代码评审

## 结论

代码评审及生产非发布验证通过，发布事项已关闭；真实 TikTok 原片帖子效果由用户后续测试。

## 评审范围

- `features/tt_gpu/worker.py`
- `scripts/test_tt_gpu_worker.py`
- `deploy/tt-post.env.example`
- `deploy/tt-post-gpu.env.example`

## 问题清单

| 编号 | 级别 | 问题 | 修复 | 状态 |
| --- | --- | --- | --- | --- |
| CR-001 | P0 | 原片 URL origin 未验证，不能直接发布 | 保持字节不变地镜像到现有已验证 COS origin，发布逐次核对实际 origin | 已关闭 |
| CR-002 | P0 | source_direct 可能仍执行 clean normalize | 在进入 GPU/FFmpeg slot 前显式分支，直接使用 `source.mp4` 作为上传对象，并由测试断言无 FFmpeg | 已关闭 |
| CR-003 | P0 | manifest 只记录输出，无法证明原字节一致 | v6 要求 source SHA/size 与 output SHA/size 完全一致 | 已关闭 |
| CR-004 | P1 | 44.1kHz 真实素材会被旧 48kHz 合同拒绝 | 仅 source_direct 接受 44.1/48kHz；其他模式保持 48kHz | 已关闭 |
| CR-005 | P1 | source_direct 配置误设 trim 会静默改变素材 | 下载前返回 `source_direct_trim_forbidden` | 已关闭 |

## 编译与验证结果

- Python compile：通过（缓存重定向到独立临时目录）。
- GPU worker：70/70 通过。
- `git diff --check`：通过。
- 全部 TT Python 回归：403/403 通过，其中 GPU 70/70。
