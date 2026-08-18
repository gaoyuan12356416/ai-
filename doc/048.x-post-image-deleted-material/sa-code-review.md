# SA 代码评审

## 结论

通过，可进入 GitHub-first 部署。

## 评审范围

Selector 策略隔离、图片媒体守卫、预检/最终发布一致性、历史错误重检和测试。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | `service.py` W2A | 图片冻结时长 0 被旧视频最小时长校验拒绝 | 只允许精确 0 映射 short，继续拒绝 0 到 0.5 秒视频 | 已修复 |
| CR-002 | P0 | 历史错误重检 | 将旧通用错误直接设为非阻断会误放真正缺失素材 | 仅允许候选扫描重检，UI/建计划前仍保持阻断 | 已修复 |
| CR-003 | P1 | 最终发布 | 仅测组件不足以证明最终类别和归因 | 增加完整 mock publish 断言 `tweet_image` 与 `af_channel=short` | 已修复 |

## 编译 / 验证结果

- `python -m py_compile ...`：通过。
- `git diff --check`：通过。
- `python -m unittest discover -s scripts -p "test_x_post*.py" -v`：执行 436 项，435 通过，0 失败，1 跳过。
- 无真实 X API 写入；所有发布验证使用脚本化 HTTP mock。

## 独立 QA

CEO 编排的独立只读 QA 最终结论为 PASS，P0/P1/P2 均为 0。其额外执行 10 项定向回归和对抗矩阵：真实 ffmpeg 生成的 JPG/PNG/WEBP/GIF 均通过真实 ffprobe；活动图片、活动视频、软删除视频通过；删除图片和未知类型拒绝；3 组 MIME/媒体类型错配拒绝；软删除视频正常调用视频 probe 一次且 repair 零次。
