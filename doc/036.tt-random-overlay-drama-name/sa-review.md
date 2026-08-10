# SA 需求与设计评审

## 结论

通过。采用独立 profile、资产哈希清单和确定性配方，可同时满足排重、重试幂等和旧方案可回滚。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-01 | 高 | CPU/GPU profile | 两套 CPU 与 GPU 分支不同，单边切换会 `prepare_profile_mismatch` | 从实际生产提交合并共同基线，三方成对切换 | 已接受 |
| SA-02 | 高 | 随机配方 | 进程随机会导致失败重试换配方 | 由不可变 job 身份和资产集 SHA 确定性派生并写 manifest | 已接受 |
| SA-03 | 高 | 黑底 GIF | 普通 overlay 会形成黑色矩形 | 光效黑键柔化；5% 视频恢复 RGB 后限制最终 Alpha | 已验证原型 |
| SA-04 | 中 | 资产交付 | 单文件超过 GitHub 普通文件上限 | 大资产放 GPU 数据盘版本目录，Git 保存构建器和哈希清单 | 已接受 |
| SA-05 | 高 | 剧名宏 | 空剧名静默替换会产生错误文案 | 实际渲染阶段 fail closed，禁止用 Drama ID 回退 | 已接受 |

## 决策记录

- 新 profile 不复用 `direct_outro` 名称或资产。
- 不把随机参数从 CPU/API 传入 GPU，避免请求伪造 filter graph。
- 不以真实 TikTok 发帖作为部署验收手段。

## PM 修订确认

以上意见已写入 requirements.md。
