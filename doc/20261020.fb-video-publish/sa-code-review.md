# SA 代码评审

## 结论

本轮SA评审以当前工作树为准。已发现的11项可复现P1/P2均已修正并复核，本轮范围内暂无未修的阻断项。此结论仅覆盖下列静态审查、离线测试与只读SQL验证，**不代表已完成生产部署、真实Graph发布、GPU产出或公网短链验收**。

## 评审范围

`features/fb_video_publish/core.py`、`service.py`、`guards.py`、`worker.py` 的事务、租户/创建者隔离、CAS版本、摘要、幂等、旧任务互斥和未知结果边界；`gateway.py`、`client.py`、`query_broker.py`、`navigation.py` 及集成patcher的权限/身份/路由边界；`scripts/deploy_fb_video_publish.py` 的文件一致性和回滚行为。

来源/宏/链接、新只读SQL适配器及后续broker词法修复由本评审人实现，相关实现验证及真实SQL证据单列于 `source-link-contract.md`，不将该部分自测称为独立代码评审。broker边界测试由另一评审角色维护，覆盖全部15种实际仓库SQL。前端交互、实际部署、GPU和生产平台验收由其他角色负责。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-01 | P1 | `service.py:89` / `templates.py:77` | 预览把占位文字传给只接受真实短链的渲染器，所有含`{url}`的正常文案预检失败 | 独立只读预览渲染，先按19位真实记录ID预算校验，再显示占位 | 已修；`render_preview`已接入，真实提交仍严格验证短链 |
| SA-02 | P2 | `service.py:66` | 任一配对含`{desc}`时要求所有素材有简介，误拒其他只用剧名的配对 | 批量取元数据不强制简介，逐配对宏校验 | 已修；`need_desc=False`，core测试覆盖混合简介需求 |
| SA-03 | P2 | `core.py:66` / `client.py:route` | 服务接受短操作ID，gateway仅允许UUID形态查询；遇到响应未知后无法查询已接受操作 | 接受范围统一UUID | 已修；提交仅32hex或规范带连字符UUID，均可经gateway查询 |
| SA-04 | P1 | `features/fb_auto_posts/service.py:365` / `deploy/nginx-fb-video-short-links.conf` | 原默认短链目录与nginx真实root不一致，文件落盘成功仍404 | 默认路径及部署环境统一到既有公开根下独立facebook目录 | 已修；两处均为`/mnt/data-disk/fb-auto-post-public/s2l/facebook` |
| SA-05 | P1 | `scripts/deploy_fb_video_publish.py:254` | 完整回滚删除短链nginx路由，已发布帖文链接失效 | 任意短链账本存在时保留公开路由、文件与账本 | 已修；`links_exist`保留SHORT_SERVER与SHORT_SNIPPET；回滚结果明确返回保留状态 |
| SA-06 | P1 | `scripts/deploy_fb_video_publish.py:227` | 有新活动任务时的保守回滚直接重启共享FB服务，可能中断旧任务 | 关闭新入口/worker；共享服务仅在旧任务排空后重启 | 已修；暂停原活动timers，`active_work()==([],0)`才重启，否则返回等待排空；恢复原timer状态 |
| SA-07 | P2 | `sources.py:search_materials` | UI承诺按剧名搜索，SQL原先只检索素材ID/素材名 | LIMIT前同content_id/语言/app/上架状态EXISTS检索剧名 | 已修；新SQL已真实只读验证，标题匹配成功 |
| SA-08 | P1 | `runtime_source.py` / `query_broker.py` / `features/fb_auto_posts/service.py:360` | 新自动化素材读取沿用直连MySQL会绕过FIFO；而专用publisher用户又无FIFO socket权限 | root主API认证loopback broker运行FIFO，sidecar仍用原专用用户 | 已修；新Page/素材/凭证均通过BrokerReadOnlyMySQL；固定副本、只读校验、并发及超时已测试；FIFO CLI实际单行读取2.100秒成功 |
| SA-09 | P1 | `query_broker.py:validate_query` | broker将合法`REPLACE()`语言规范化函数当DML禁止，所有素材查询被拒 | 仅拒绝REPLACE INTO，保留单条SELECT和表白名单 | 已修；实际7类仓库SQL均经白名单测试通过 |
| SA-10 | P1 | `query_broker.py:_validate_sources` | 逗号连接、注释连接、TABLE子查询或带引号函数绕过只扫描FROM/JOIN的表白名单 | 受限SQL词法：每个字符必须识别、按层跟踪FROM、精确表与函数白名单 | 已修；仅允许8秒hint，其余注释/逗号表列表/派生表/未知函数/TABLE子查询拒绝；18项broker测试含深层绕过及15类真实模板均通过 |
| SA-11 | P1 | `runtime_source.py:_bind` | `CONVERT(0x… USING utf8mb4)`参数的IMPLICIT排序规则和真实unicode_ci列冲突，完整素材查询报MySQL1267 | 使用`_utf8mb4 0x…`可强制转换的字符串字面量，继续保持UTF-8十六进制注入边界 | 已修；真实FIFO完整素材/剧名/简介查询成功，总8.868秒；10秒候选仅写本地评审文件 |

## 已核对的关键边界

- `core.submit`使用`BEGIN IMMEDIATE`，在同事务重新比较完整actor/request/source/config/repeat摘要、版本和配额；每配对分配独立任务与短链记录并冻结文案。真实短链长度参与5000 UTF-8字节校验，任一失败整体回滚。
- SQLite回滚可能重用尚未提交的AUTOINCREMENT值，但事务中不写文件或发网络请求，worker仅处理已提交账本，所以不会导致既有短链被重新指向。已提交记录与文件不删除、不重分配；文件内容不符拒绝覆盖。
- Page配置租户共享并经源Page权限过滤；草稿及批次按tenant+owner隔离，管理员也受tenant范围。配置删除再创建使用新instance，旧任务保留原版本快照。
- `guards.install_backstops`为新旧表安装双向SQL守卫；应用层在创建及执行领取事务内检查相同真实Page的旧/新活动与unknown占用，不按tenant切割真实Page锁。共享GPU容量按同一SQLite事务计数。
- `Worker.link_next`先核对账本短长链身份，再不可覆盖落盘和SHA读回，随后固定公开短链HTTP 200及内容SHA一致才可标为ready；不跟随落地页跳转。执行需要link与prepare均ready。
- Graph调用之前持久化attempt意图；超时/无对象ID/未知结果停止轮换，后继Page任务继续受unknown锁；仅已确认的凭证失败可换健康凭证。过期running进入unknown，不盲目重发。
- 主API仅Cookie会话、模块和入口权限通过后构造可信actor；前端身份字段被丢弃。邮箱映射要求唯一有效sub_user_id。记录/取消不依赖再次成功映射邮箱。
- 内部查询broker独立检查loopback、无转发头、恒时比较内部Bearer、固定schema/table白名单及JSON长度；公网nginx拒绝该路径。SQL与错误中的密码/token不进入响应或日志。新Page凭证只在私有进程链路读取，不返回浏览器。

## 编译 / 验证结果

2026-10-20本轮执行：

- `python -m unittest tests.test_fb_video_source_links -q`：29项通过。
- `python scripts/test_fb_video_publish_core.py -q`：34项通过。
- `python scripts/test_fb_video_publish_gateway.py -q`：9项通过。
- `python scripts/test_fb_video_publish_deployment.py -q`：2项通过，覆盖短链路由保留及旧任务未排空不重启。
- `python scripts/test_fb_video_publish_broker.py -q`：18项通过；此前白名单绕过已修、AST fixture已改为真实多行形式，patcher marker也独立占行。
- 负责文件`py_compile`及`git diff --check`通过。FIFO完整查询验证了真实排序规则兼容，避免把单行主键探测误当作全部来源SQL验证。
- 来源查询在真实只读副本验证2条DTO和新增剧名筛选；原始时长、返回字段、每条SQL耗时记于来源合同。没有运行真实Graph、短链创建或GPU调用。

部署后仍需按QA清单核实实际systemd环境、broker HTTP闭环、0750/0640及nginx ACL、公开短链SHA与权限入口。此处不以本地测试替代这些验收证据。
