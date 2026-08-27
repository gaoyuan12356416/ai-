# BUG-GCP-003 图片视频基准混入Campaign AF

## 发现阶段

2026-08-27新政策发布前审查；线上尚未切换。

## 现象与根因

GG平台消耗/曝光已经改为图片视频池，但build_month_payload原本从af_totals读取整个Campaign平台的AF首交，可能把例如NG 124815次、PK3820次用于新素材池的CPA/APM。基准分子、分母范围不一致。

## 修复

GG图片视频基准没有精确AF，af_d0_first_transactions、CPA/APM、cpa_finite、audit.af_total留null，evidence.platform_cpa_available=false；不使用0或全Campaign归因替代。Meta/TT原逻辑不变；GG A/B只取CPC/CTR，入选集合不受影响。

## 验证

新增注入124815次Campaign AF的单元测试；独立验收器强制GG基准AF/CPA为null。152项后端用例、七个月独立raw-cache核对、7月46条审批快照、Meta/TT186行所有字段守恒均通过。修复版本c3a39dc于2026-08-27 17:33上线；未将错口径AF版本发布到生产。

浏览器收尾发现旧前端把所有不可用CPA都解释为“平台USD基准不完整”。本轮补充仅对新GG政策改成“图片/视频素材池无同口径AF数据”，保留旧快照/其他渠道说明；新增前端回归用例，50项行为测试通过。此提示修正不改变JSON、CSV数值或入选结果。
