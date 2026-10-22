# SA 代码评审

## 结论

UI / 主 API 契约审查及独立后端审查完成。首次独立审查的4项恢复、权限、错误映射问题均已最小复现，并在修复后重放通过。后续P1缺口BUG-006已完成本地修复与新增9项回归，独立只读审查未发现阻断问题；确切提交部署及线上回读待主实施。真实COS、GPU合成素材两条成片及完成态隔离停启恢复已通过；正式Chrome双真实剧出片仍在进行中，浏览器文件选择受扩展file URL权限阻挡。部署事实由主实施记录。

## 评审范围

- 新增视频剪辑页面、脚本、样式；现有 QuickNav / UiTopbar 登录与导航接入。
- 对照 `features/video_editing/routes.py,service.py,uploads.py,catalog.py,recipe.py,worker.py,store.py` 核验请求方法、JSON DTO、来源身份、产品权限、普通用户隔离、状态、单位、限额及恢复语义。
- 两个 JS 测试文件中的纯剪辑模型与真实事件 / API 异步契约测试。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| UI-01 | 高 | 保存 / 项目切换 | 保存过程中新增编辑可能在换项目时丢失；旧读取响应可能覆盖新选择 | 换项目先 flush；写响应绑定原 project；读取使用请求序号 | 已修复，异步测试通过 |
| UI-02 | 高 | create / PATCH 网络未知 | 失联后新建操作内容变化可能重复创建；PATCH 重发可能盲增版本 | create 冻结 op + payload；PATCH GET 回读核对 | 已修复，异步测试通过 |
| UI-03 | 高 | 真实版本冲突 | 不得用本地版本覆盖另一编辑者内容，也不能让草稿无法恢复 | 保留本地草稿并中止换项目；显式“另存副本”保留修改 | 防覆盖与另存副本测试通过 |
| UI-04 | 高 | 分片直传恢复 | 403 后重复建上传可能造成对象重复；凭证不能进入持久存储 | reconcile 原会话、同 MD5 重签；localStorage 只存元数据 | 已修复，multipart 契约测试通过 |
| UI-05 | 中 | 时间线 / GPU 配方 | 毫秒时长与 GPU 30fps 端点取整不同；极短片可在 UI 有效但 GPU 拒绝 | 按同一帧算法计算时长 / 淡化容量 | 已修复，帧边界测试通过 |
| UI-06 | 中 | 来源 / 任务 DTO | Unix 秒当毫秒显示日期，恢复状态缺少操作，开关关闭隐藏历史 | 统一秒转换；原 task resume；接单暂停保留历史与续传 | 已修复，代码核对 |
| API-01 | 高 | render 配方 | 恰好一个素材片段限制与允许重复插入冲突 | 至少一个素材片段，可多次引用同一 source | API 实施方已修复，当前代码复核 |
| UI-07 | 中 | 剧集批量选择 | probe 每次最多100 IDs；已有20剧时仍应允许添加既有剧的剧集 | 100 IDs 分批；限制新剧数；单剧300片提前提示 | 已修复，代码核对 |
| UI-08 | 中 | 停止接单后的未知上传 | 无 upload ID 但已保存 op 的旧初始化结果仍需读回 | 允许携带原 op 请求后端幂等核实，由后端拒绝真正新建 | 已修复，代码核对 |
| UI-09 | 中 | 产品展示与目录查询 | 实际产品 DTO 为 label / app；空查询触发剧集表长扫 | 按 label / app / app_id 展示；要求完整 ID 或至少2字前缀；后端索引查询加预算 | 已修复，新增2项 JS 契约测试；见 BUG-002 |
| UI-10 | 中 | GPU进度DTO | runtime阶段及dict指标未展示，容易裸露技术英文或误算整体进度 | 补齐中文阶段；仅显示当前片段时间、当前传输量或步骤数，保留numeric兼容 | 已修复，3项实际DTO契约测试通过 |
| UI-11 | 中 | 提交后撤销 / 剧集probe恢复 | 撤销复用旧render操作号；原probe异常后页面无法持续跟踪 | 撤销清空操作号；恢复原probe task并跟踪终态，不新建替代任务 | 已修复，两剧完整事件链路与恢复跟踪测试通过 |
| UI-12 | 中 | 预览链接与异步响应 | 未采用实际expires_at字段；离开项目后迟到响应可能显示旧视频 | 检查expires_at并续签；停止/切换时推进预览代次并隐藏空时间线视频 | 已修复，过期与迟到响应测试通过 |
| IND-01 | P2 | worker 恢复状态竞争 | GPU GET 发出后用户恢复原任务；旧 failed / running 响应可将 resume_queued 覆盖，恢复请求丢失 | 事务内复查当前状态，拒绝覆盖新 resume_queued，completed 回执优先 | 已复现，修复后原重放保留 resume_queued |
| IND-02 | P2 | uploads 完成 / 取消崩溃窗口 | 完成状态先落账后进程退出，原上传永久停在 completing；complete / abort 均不可推进 | 记录操作开始时间；120秒后 reconcile 转 unknown，核查原 UploadId 与清单 | 已复现；过期 completing → completing_unknown → completed 原会话通过 |
| IND-03 | P2 | service 旧 episode source | 产品已不在当前目录时，直接引用旧 source 仍可保存 / 提交新任务，绕过目录入口门禁 | 每次 episode source 读取核对当前产品；保留已冻结任务读取 / 恢复 | 已复现；修复后旧 source 返回 product_not_available |
| IND-04 | P2 | worker → UI 错误 DTO | runtime 返回嵌套 error.code，CPU 只读顶层 error_code，磁盘等具体错误全部丢成 gpu_render_failed | 兼容嵌套与旧字段，安全错误码加固定中文映射 | 已复现；video_editing_disk_budget 正确保留；实际转场码已复核 |
| IND-05 | P1 | 同项目同版本重新提交 | 刷新后新operation_id未命中原批次，会新建逐剧任务重复制作 | 服务端按冻结项目版本持久防重，并保留既有操作号 / 原任务恢复语义 | 本地已修复，新增9项回归与独立只读审查通过；部署回读待主实施，见BUG-006 |

独立审查由前端实施人员以只读方式检查后端，修复由后端负责人完成，再使用原最小用例复测；没有直接修改其他实施者的 Python 文件。其余已检查路径包括共享 ready 素材可读但仅 owner / admin 可管理，项目 / 任务 / 上传以租户及用户身份隔离，客户端不能自带来源 URL，分片内容 MD5 不可替换，已完成任务不可再次恢复。没有把静态检查当成真实多人权限操作证据。

新增 `ve_project_version`、`ve_batch`、`ve_output` 与 task DTO 的稳定 ID 已核对：相同 operation_id 从原批次取回任务；当前草稿变化或素材软删除不触发成功任务重制；新提交仍核对 expected_version 和来源权限。

BUG-006修复独立只读审查：新增 `ve_render_operation` 按调用者身份及外部操作号保存映射；先查原映射和历史原操作，再按project + version复用最早批次，保留历史重复批次及原操作的返回身份。查找、映射和新批次 / 任务 / 成片创建均处于同一 `BEGIN IMMEDIATE` 事务，异常整体回滚。新任务 / 批次 / 成片归项目owner，`submitted_by` 记录实际提交者；历史管理员任务不改归属，无权项目owner复用时返回409 `batch_task_permission_conflict`。已完成、失败和未知状态复用不触发状态重置，失败仍通过原task恢复。审查当前service / store差异与新增9项用例未发现阻断问题；不将代码审查替代线上回读。

## 编译 / 验证结果

2026-10-22：`node --check static/video-editing.js` 通过。

`node --test tests/test_video_editing_frontend.js tests/test_video_editing_browser_contract.js`：23项通过、0失败（模型9、浏览器事件/API契约14）。测试覆盖独立MD5对照（含完整16MiB）、30fps对齐、淡化 / 多段裁剪 / 重排 / 公共套用、真实DTO与阶段进度、完整两剧编辑提交成片链接链路、原probe恢复、过期/迟到预览、保存竞态、未知结果、冲突后另存副本、分片续传和凭证不落localStorage。

独立复测后端service / uploads / routes子集：41项，40通过、1项可选COS SDK跳过、0失败。BUG-006修复后，后端负责人和主实施分别完成Python117项，116通过、1项可选COS SDK跳过；其中service子集29项全部通过。diff检查与Python3.9 AST检查通过；旧模块回归215项，209通过、6跳过。不同阶段和范围不重复累计，详见测试报告。

真实浏览器交互由主实施者在本地 `?demo=1` 页面验证：第一部剧删除选中片段后4片/50秒变3片/37.7秒；第二部仍3片/52秒；撤销后恢复。该证据为模拟 UI 交互，不是生产素材制作结果。

后续正式Chrome已执行真实剧集校验、裁剪、两次切割删除中间内容、整集删除/撤销及重排；双真实剧已提交，完成回执与成片检查仍在进行中。系统验收标准服务直传通过，但未绕过Chrome扩展file URL权限，也不声称浏览器文件选择已通过。

隔离任务 `ve_4483dabb553b53369d18aff38f18bd6f` 完成后独立listener停启、GET与重复POST命中缓存已实测；generation=1、COS身份与账本hash不变，正式GPU/PID及旧23份记录不变、CPU claim恢复。此证据是完成态恢复，不是运行中kill注入；原始证据见 `final-backend-review.md`。
