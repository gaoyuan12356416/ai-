# YouTube 封面同步素材库

2026-10-19 用户授权：沿用单张样本验证通过的 ai-source 接口；存量包含所有已审核通过的最终封面（包括待发布、失败或结果待确认的视频任务）；后续仅在 YouTube 发布成功后同步最终封面。保留所有原发布状态。

## 业务规则
- 产品接受接口实际落库值 `Drama-AI素材`，包名 `com.dramawave.app`。沿用人员邮箱到 admin_user_group 的唯一用户名、人员 ID 映射。
- 存量清单冻结任务、版本、资产、SHA256、剧目、所有者及真实尺寸；不包含未审核或被打回版本。只读预查 730 张，最终以服务器冻结清单为准。
- 同步原始最终图片字节。横图分类16:9，原有正方形分类1:1，不修改私有源图。
- 后续要求 reviewed_thumbnail、video_state=published、无 unknown_outcome、真实 video_id，以及 public/thumbnail/processing 均 succeeded。首评失败不否定已发布视频。
- 任务+封面版本+资产生成稳定素材外部ID。复用已完成的单张试传，避免重复上传。

## 技术设计
独立 systemd oneshot+timer，每次最多50张，独立SQLite outbox。源数据库只读，禁止导入主 app 或调用 YouTube API。COS存储沿用已验证域名/桶，物理对象包含任务/版本/资产。API接受后查询 data_source=102 接收行的 remark 指向，验证正式 data_source=6 图片记录及人员、剧目、产品、比例。

## 异常
网络调用前持久化 unknown 和 attempts=1；进程崩溃、超时、非成功响应只对账，不盲目重发。目录/SHA/归属不符或人员映射不唯一阻断并记录。公共COS对象通过长度及MD5回读。全程不恢复历史视频任务、不发视频或通知。

## 验收
全部冻结清单得到唯一正式素材ID；试传记录被认领；再次运行无重复POST；后续发布扫描生效；现有API/发布worker PID保持，原数据库不写；GitHub发布、服务器测试、独立服务健康及回滚均留证。
