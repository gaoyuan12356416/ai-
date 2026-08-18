# 测试用例

## 测试范围

源选择、历史错误重检、图片下载/探测、媒体上传类别、预检、最终发布、防回归和生产只读验收。

## 测试数据

Mock JPG/PNG/WEBP/GIF、活动图片、软删除视频、已删除图片、X Auto 素材和现有 140 秒边界视频。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 活动图片入池 | type=1,is_delete=0 | selector pool/manual | 通过并标记 image | P0 | 通过 |
| TC-002 | 软删除视频入池 | type=2,is_delete=1 | selector pool/manual | 通过并保留视频规则 | P0 | 通过 |
| TC-003 | X Auto 隔离 | 图片或软删除视频 | auto selector | 保持拒绝 | P0 | 通过 |
| TC-004 | 图片大小 | 图片大于5MiB | 下载/探测 | X 写入前失败 | P0 | 通过 |
| TC-005 | GIF 大小/类别 | 合法 GIF | 预检/上传 mock | 15MiB 上限，tweet_gif | P1 | 通过 |
| TC-006 | 图片 MIME 欺骗 | 声称图片但 ffprobe 非图片 | probe | fail closed | P0 | 通过 |
| TC-007 | 图片预检 | 合法图片 | `_preflight_candidate` | 时长0，不调用视频 probe/repair | P0 | 通过 |
| TC-008 | 最终图片发布 | 指纹一致 | publish mock | tweet_image + 一次 create Post | P0 | 通过 |
| TC-009 | 视频回归 | 短/长/Premium relay | 既有测试 | 行为不变 | P0 | 通过 |
| TC-010 | 历史错误重检 | 三个旧错误码 | available query + selector | 可重检并清除/精确替换 | P1 | 通过 |
| TC-011 | 生产无真实 Post | 部署窗口 | ledger/timer/health | 无意外 Post、unknown=0 | P0 | 待执行 |

## 回归范围

X pool、manual、schedule、catch-up、Premium relay、URL channel、ledger、OAuth sidecar、主 API。
