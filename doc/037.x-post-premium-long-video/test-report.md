# 测试报告

## 测试结论

本地实现验证通过，可以进入 GitHub-first 部署。未创建真实 X Post。

## 测试范围

- token 会员枚举、缺失/未知降级、SQLite 加法迁移与安全 DTO。
- 素材源 600 秒上限、最新优先、混合账号长短素材匹配和无会员留池。
- `tweet_video` / `amplify_video`、发布前会员降级、队列时长指纹。
- GPU `standard|premium` 修复策略、profile/job key v3、CPU 二次验证。
- X 账号、素材/短剧池、补发、随机/固定排期、全局去重、页面契约回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| X 全量 unittest discovery | 381 | 380 | 0 | 0 |
| 既有环境相关 skip | 1 | 0 | 0 | 0 |
| py_compile / diff check | 2 | 2 | 0 | 0 |
| 生产部署验证 | 待部署 | — | — | — |

## 缺陷情况

- 已修复：素材选择器遗留 `video_duration<=140`，导致长素材在媒体预检前被过滤。
- 已修复：素材选择器仍按最旧优先重排，与当前 `created_at DESC,id DESC` 池合同冲突。
- 已修复：候选文案预检未按最新 `build_post_text` 参数传入 `drama_name`。
- 当前无未解决的本地 P0/P1 缺陷。

## 验证证据

```text
python -m unittest discover -s scripts -p "test_x*.py"
Ran 381 tests in 52.214s
OK (skipped=1)
```

聚焦套件：账号 56、发布 35、每日/路由 55、GPU 修复 17，均通过。

`py_compile`（8 个生产入口）与 `git diff --check` 均为退出码 0。

## 遗留风险

- `amplify_video` 对这些个人会员账号的最终平台兼容性只能由后续自然长视频调度证明；本次不为测试创建 Post。
- 会员资格动态变化；缺失、未知或发布前降级均会失败关闭，可能让长素材延后。

## 发布建议

按部署文档先备份并演练 SQLite 迁移，再部署同一 Git commit 到 GPU/CPU；同步 5 个正式账号的安全会员快照，核对 queue/log 计数不变后恢复自然调度观察。
