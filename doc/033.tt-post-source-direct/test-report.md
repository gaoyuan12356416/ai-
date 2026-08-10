# 测试报告

## 测试结论

通过。全部 TT Python 405/405 回归与生产原任务自然重试验收均已完成。

## 测试范围

GPU source_direct/config/prepare/manifest/publish mock；既有 GPU 媒体模式、COS/local 存储、门禁、幂等和核对回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: | ---: |
| source_direct 定向 | 5 | 5 | 0 | 0 |
| GPU worker 全量 | 72 | 72 | 0 | 0 |
| 全部 TT Python 回归 | 405 | 405 | 0 | 0 |
| 生产验收 | 1 | 1 | 0 | 0 |

## 缺陷情况

确认并修复 `source_direct` 误拒绝 HEVC Main/`hvc1` 原片的问题。首次定向运行遇到本机既有 `scripts/__pycache__` 权限冲突，改用独立 `PYTHONPYCACHEPREFIX` 后通过；该问题不属于产品代码。

## 验证证据

- source_direct 测试证明 H.264/`avc1` 与 HEVC/`hvc1` 均保持无 FFmpeg，源/输出 SHA 和大小相等，manifest v6 可复用，mock 发布只 init 一次；错配 tag、VP9 和 HEVC Main 10 继续拒绝。
- GPU 72/72 通过，覆盖既有 branded/clean/outro、COS/local、发布账本、URL Property 和未知结果规则。
- TT Python 发现集 405/405 通过，覆盖账号设置、发布池、准备 runner、页面/API 合同、短码与链接。
- 生产任务 34 保留原 `gpu_job_id` 和素材 `6028067` 自然重试：HEVC Main/`hvc1`、AAC-LC 44.1kHz、720×1280、107.6 秒通过 prepare；源/输出 SHA-256 均为 `174499dbbd339b44083082c96ac721567399f7b2be6b30a9c2d16748a8816ff4`，大小均为 `18,691,270` 字节。
- 同一任务 `publish_attempt_count=1`，取得 `publish_id=v_pub_url~v2-1.7671247289059182610` 后只进入 reconcile，并于 2026-08-07 19:19:04 CST 收敛为 `published`；`unknown_outcome=0`。
- 生产 GPU release 为 `7e428f57786b0337451d081297cfa55800935497`，服务健康、三项发布闸门保持开启；旧 TT Post 服务未重启。

## 遗留风险

- 本次真实发布验证覆盖一条 HEVC Main/`hvc1` 原片；其它未列入合同的编码/profile/tag 组合仍会 fail-closed。
- 非标准源素材需要切回制作模式或另行评审放宽合同。

## 发布建议

已完成 GitHub push、GPU 数据盘备份、不可变 release 切换、生产 health 和原任务自然调度验证。部署过程没有创建替代任务，也没有主动重复 TikTok init。
