# BUG-GCP-003 图片视频基准混入Campaign AF

## 发现阶段

2026-08-27新政策发布前审查；线上尚未切换。

## 现象与根因

GG平台消耗/曝光已经改为图片视频池，但build_month_payload原本从af_totals读取整个Campaign平台的AF首交，可能把例如NG 124815次、PK3820次用于新素材池的CPA/APM。基准分子、分母范围不一致。

## 修复

GG图片视频基准没有精确AF，af_d0_first_transactions、CPA/APM、cpa_finite、audit.af_total留null，evidence.platform_cpa_available=false；不使用0或全Campaign归因替代。Meta/TT原逻辑不变；GG A/B只取CPC/CTR，入选集合不受影响。

## 验证

新增注入124815次Campaign AF的单元测试；独立验收器强制GG基准AF/CPA为null；重新生成新缓存快照并复核7月46条和全部旧渠道。待本轮完整回归记录于release文档。
