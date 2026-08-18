# SA 评审意见

## 结论

通过。必须保持路径隔离：素材池/manual 放开，X Auto 与 drama pool 不继承；图片必须有独立大小、解码和上传类别守卫。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | selector | 直接删除 `type/is_delete` 条件会连带放开 X Auto | 增加显式策略参数，仅 pool/manual 开启 | 已采纳 |
| SA-002 | P0 | publish | 仅放开入池会在视频 probe 处失败 | 增加图片探测和 `tweet_image/tweet_gif` 分支 | 已采纳 |
| SA-003 | P1 | 历史池记录 | 旧错误默认不再参与扫描 | 将旧错误作为可重检证据 | 已采纳 |

## 决策记录

- 不增加数据库列，以下载 MIME + 指纹和 `preflight_duration=0` 区分图片。
- 已删除图片仍拒绝；仅软删除视频获得豁免。

## PM 修订确认

requirements.md 已吸收全部意见。
