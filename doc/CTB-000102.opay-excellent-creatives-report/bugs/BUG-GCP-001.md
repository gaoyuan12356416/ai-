# BUG-GCP-001 GG 视频经 YouTube 回连链遗漏

## 发现与根因

2026-08-27 线上7月仅2张图片。只读核验发现视频采用 `mapping → source(type6) → youtube_videos → original source(type3) → custom_source`；旧版仅接受直接type3，导致合法视频被记为 invalid_source。

## 修复与验证

新增完整桥接，逐一验证所有候选、真实最终素材ID、App、素材类型、original_source_id和非空video_id。中间YouTube ID不作为素材ID，重复广告不倍增事实。保留原4字段映射证据并追加桥接证据。

`test_google_youtube_mapping.py` 27项通过；非实现者独立复核无阻断项。源码使用主键连接，不连接事实表做汇总。生产7月全资产独立源库核验及上线结果见测试报告后续记录。
