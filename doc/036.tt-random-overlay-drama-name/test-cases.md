# 测试用例

## 测试范围

宏语法/冻结、资产校验、配方边界/幂等、FFmpeg 命令、GPU manifest、CPU/GPU profile 对齐和历史回归。

## 用例列表

| 编号 | 场景 | 预期结果 | 优先级 |
| --- | --- | --- | --- |
| TC-01 | `{{drama_name}}` 正常剧名/中文/Emoji | 两套入口渲染一致且 UTF-16 限制正确 | P0 |
| TC-02 | 使用剧名宏但剧名为空 | 稳定错误，任务/队列不创建 | P0 |
| TC-03 | 未使用剧名宏且剧名为空 | 保持原有模板可用 | P1 |
| TC-04 | 资产 manifest 缺类、多余路径、哈希不符、符号链接 | GPU 启动失败关闭 | P0 |
| TC-05 | 同 job 重复 prepare | recipe、输出 identity 一致并复用 | P0 |
| TC-06 | 不同 job | 四类均选中且配方通常不同 | P1 |
| TC-07 | 随机数边界 | 角度、缩放、透明度全部在闭区间 | P0 |
| TC-08 | 光效类禁用 | 配方、输入文件和 filter graph 均不含光效 | P0 |
| TC-09 | 黑底 5% 视频 | RGB 恢复，最终 Alpha 不超过约 5% | P0 |
| TC-10 | 动画短于源视频 | 循环到源视频结束 | P1 |
| TC-11 | 98% + 旋转 | 未变形原帧补边，无黑边 | P0 |
| TC-12 | 无音频源 | 延续现有静音 AAC 补齐契约 | P1 |
| TC-13 | 新 profile/trim mismatch | CPU/GPU 启动或 prepare fail closed | P0 |
| TC-14 | `source_direct`/`direct_outro` 回归 | 旧模式测试不变 | P0 |
| TC-15 | 离线完整合成 | 720x1280/30fps/yuv420p/AAC-LC，时长匹配 | P0 |
| TC-16 | 光效资产兼容 | 两个光效资产仍保留在不可变资产集以便回滚，但均不进入新配方 | P0 |
| TC-17 | 模板音频审计 | 原始 GIF/PNG 与转换资产均无音频，成片只映射源素材音轨 | P0 |

## 回归范围

执行全部 `scripts/test_tt*.py`，并核对旧素材池、自动发布、GPU source-direct/HEVC/片尾测试。
