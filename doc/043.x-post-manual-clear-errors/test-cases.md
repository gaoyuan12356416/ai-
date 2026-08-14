# 测试用例

## 测试范围

selector、GPU 修复校验、manual runner 错误归一、页面展示和既有 X 发布回归。

## 测试数据

全部为离线 fixture/mock；不访问 X 写接口。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 素材不存在 | exact ID 无源行 | selector 检查 | `material_not_found`，中文含素材 ID | P0 | 通过 |
| TC-002 | 时长为0 | `video_duration=0` | selector 检查 | `material_duration_missing` | P0 | 通过 |
| TC-003 | 超4小时 | `video_duration=14401` | selector 检查 | `material_duration_exceeds_limit` | P0 | 通过 |
| TC-004 | 非视频 | `type<>2` | selector 检查 | `material_not_video` | P0 | 通过 |
| TC-005 | 已删除 | `is_delete<>0` | selector 检查 | `material_inactive` | P0 | 通过 |
| TC-006 | 修复后空文件 | output size=0 | worker 校验 + runner 归一 | 手动 run 归类 `repaired_media_empty` | P0 | 通过 |
| TC-007 | 修复后超限 | output size>limit | worker 校验 + runner 归一 | 手动 run 归类 `repaired_media_too_large` | P0 | 通过 |
| TC-008 | 时长不足/超限 | 0.49秒或超过账号上限 | worker 校验 | 明确中文时长原因 | P0 | 通过 |
| TC-009 | 历史 #17 形态 | coarse code + size 英文消息 | UI 映射 | 仅显示“修复后视频超过512MB上限” | P0 | 通过 |
| TC-010 | 新时长缺失 | 具体 run 码+素材 ID | UI 映射 | 显示素材 ID和0秒原因，不显示错误码 | P0 | 通过 |
| TC-011 | 未知错误 | 未映射英文错误 | UI 映射 | 显示联系技术人员，不回显内部文本 | P1 | 通过 |
| TC-012 | 发布安全 | 部署前后账本基线 | 只读核对 | queue/log/Post 不因部署增加，unknown=0 | P0 | 待生产验收 |

## 回归范围

手动立即/定时任务、素材池 selector、GPU 修复、X daily/schedule/ledger、静态页脚本语法。
