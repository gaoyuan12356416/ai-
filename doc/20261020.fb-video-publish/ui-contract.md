# Facebook 视频发布 UI 合同

2026-10-20。正式页面复用真实 Cookie、QuickNav 和 UiTopbar；不读取 COS 原型 localStorage、不内置生产示例列表。

## 交互和所有权

- Page 配置按 tenant + PageID 共用，只允许当前能访问 Page 的用户查询和写入；保存和删除提交配置实例与版本。列表初始可为空；多选创建保存后每 Page 一行。冲突保留编辑输入，不自动覆盖。
- 批次、草稿及记录按 tenant + 当前发布人归属，同租户管理员可查看。用户切换期间不得显示上一个登录身份的草稿。
- 素材分页加载、跨页/搜索保留选择；选择集合由素材 ID 标识，移除单项显式操作。只支持素材库，不提供本地文件上传。
- 已配置且有权且授权正常的 Page 可选；先选择范围，再按剧语言建立对应关系，支持手动增减。任一已选素材无配对时禁止确认。
- 每个素材 × Page 配对独立文案；重匹配不覆盖已有编辑。素材顺序决定同 Page 内顺序；间隔为 0–1440 整数分钟。加工方式为原片 `original` 或随机加工 `random_overlay`。
- Page 管理文案以示例剧目预览，明确标注示例，不绑定发布素材。只支持 `{name}`、`{desc}`、`{url}`。
- 确认是只读预检：短链使用明确占位，显示 2310 长链规则、实际文案预览和历史成功重复项。最终提交才分配真实任务编号与真实短链。
- 提交使用稳定 operation_id。网络中断或非明确拒绝时不清空输入、不换操作编号；查原操作结果，确认接受后跳转记录。活动或未知结果硬阻断，不得被“确认重发”绕过。
- 预约输入按北京时间（UTC+08:00），请求转为 UTC；显示时间明确标注北京。后端最终复验未来时间和当前权限。
- 草稿保存服务端并支持刷新恢复；在提交失败、版本冲突、加载失败时保留输入和选择。

## 页面与权限

| 页面 | 导航 key | 权限 |
|---|---|---|
| `/fb-pages.html` | `fbPageManagement` | `fb_video_publish` |
| `/fb-video-publish.html` | `fbVideoPublish` | `fb_video_publish` |
| `/fb-video-publish-records.html` | `fbVideoPublishRecords` | `fb_video_publish` |

所有 API 请求使用 `credentials: same-origin`、`cache: no-store`。写入 JSON；401/403 在页面内明确提示，不用空数组伪装成功。字段和值均转义展示，短链链接只接受 HTTPS，禁止将错误响应中的原始 HTML 注入页面。

## 接口协作

已与后端对齐 [implementation-contract.md](implementation-contract.md)，统一前缀 `/api/fb-video-publish`。语言取 `GET /languages` 的 canonical code/label，不硬编码枚举；Page 配置与候选为 API 全量结果，已配置列表按 20 行客户端分页；素材和记录按后端 cursor、每页 50 项加载。记录列表为轻量摘要，点详情后才读取冻结任务。

`POST /preflight` 返回 15 分钟有效的 preview_token，只读不分配编号。`POST /batches` 保持原 operation_id，提交时先将原始请求与 pending 状态写入服务端草稿，浏览器 sessionStorage 仅额外保留当前操作恢复指针，不作为业务记录。

首次提交失败只有后端明确 `accepted:false` 才解除编辑锁；网络错误、未知服务错误及 operation 查询 404 都保留原操作。刷新后优先查询原 operation，已受理直接打开记录；未确认则仅允许同操作、同请求、同预检 token 幂等重试。已有未知结果时，重试返回 `accepted:false` 仍不能证明先前请求未受理，继续保留操作并查询。已受理批次忽略过期 token 回原结果；首次明确未受理的过期 token 返回 accepted:false 后可重新编辑和预检。最终提交成功后清空服务端草稿；清空失败不改变已受理事实。

确认弹窗的 `long_url_parameters` 直接展示后端八字段预检值，提交相关编号仍为占位，不由前端模拟。首次最终提交前保存草稿失败时不发送批次请求，可继续编辑；已有未知提交的重试若草稿保存失败，仍保留原操作和编辑锁，不增加提交请求。

前端不可推导当前用户/租户/业务 sub_user_id，不可发送凭证或客户端 long_url/short_url。预检、任务快照、时间安排及链接以服务端结果为准。
