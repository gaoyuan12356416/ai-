# 单个 Page 负责人访问权限

普通用户已被设为 `ads_facebook_page_ins.user_id` 负责人时，原 Page 搜索仍然只检查
个人 Facebook 授权和有效 Dramawave Page 池负责人，因此返回空候选。
本次将单个 Page 负责人作为第三个 OR 分支，使用当前登录用户邮箱唯一映射得到的
可信 `business_owner_id`，通过同一 Page ID 和负责人 ID 匹配。浏览器不能自报身份。

管理员全局范围、原个人授权严格 BINARY 匹配、原有效池负责人范围保持不变。
负责人授予与池负责人相同的 Page 委托访问范围；不改变个人 Token 模式的凭证归属。
仍从 `ads_facebook_page_post` 读取候选，使用原 status / 非空 Token 资格判断，
无源记录不能凭负责人记录生成候选，无可用授权仍显示不可用原因。
负责人的修改和撤销会在后续实时读取中生效。配置仍按同租户未删除记录与可见范围取交集。
不增加 Page 状态、产品或池成员条件；不将源表状态快照当作新的 Graph 权限证明。

`sources.py` 的共享来源用于搜索、配置访问、预检、Excel、池调度及 worker 复检。
`query_broker.py` 的精确表白名单增加 `ads_facebook_page_ins`；仅允许既有内部
loopback + bearer 只读接口访问，未更改 SQL 语法校验、数据库端口、身份或路由边界。

## 验证

`tests/test_fb_authorized_pages.py` 使用独立关系型夹具执行真实权限查询，同时通过
实际 broker 校验器，验证无个人授权/无池的负责人可见、跨用户拒绝、动态撤销及转交、
同租户配置隔离、缺少凭证仍禁用、重叠授权不重复，以及原三类规则全部保留。
另外执行 broker、source/link、gateway、worker、pool、Excel 和部署失败路径检查。
线上仅做只读请求、既有任务/短链核对，不创建配置、批次或测试发布。

本地检查：`tests/test_fb*.py` 49项通过；`scripts/test_fb_video_publish*.py`
86项通过；相关 pool/identity/Excel/manual/部署组合226项中224项通过、1项缺少
本地只读快照跳过、1项旧 Excel 稀疏格式行断言失败。后者在未修改的基线工作树
单独运行同样失败，未将无关 Excel 行数策略变更混入本次授权修复。连同后续独立
效果采集排空保护测试，合计360项通过。新增6项部署测试覆盖主 API 重启、排空失败
恢复、复制失败回退、重新开放后禁止自动缩小权限、独立采集源码核验，以及显式回退
的待执行/unknown/启用池保护。

## 部署和回滚

基线提交 `cd9e06891c24bfab3a3a1260cd07f6765556d2ec`，两个目标文件的 Git blob
与当前 CPU 两份运行文件完全一致。分支 `codex/fb-page-owner-access-20261026`。
部署脚本 `scripts/deploy_fb_page_owner_access.py` 只替换 `sources.py` 与
`query_broker.py` 在 `/root/drama_material_service` 和 `/opt/fb-auto-post/current`
下的四个副本。无数据库迁移、配置写入、静态改动或 GPU 改动。

提交并推送 GitHub 后，CPU 从 GitHub 获取精确提交，在数据盘 release 目录运行：

```bash
python3 scripts/deploy_fb_page_owner_access.py
python3 scripts/deploy_fb_page_owner_access.py --apply
```

脚本验证推送 ref、数据盘、四个基线哈希及素材池时点，保存原 timer/worker 状态，
排空实际执行工作，在线备份 SQLite 和四份文件。重启主 API 以加载新表白名单，
重启 FB sidecar 以加载新查询，之后恢复原服务状态；不主动重试历史或 unknown 任务。
独立效果采集不导入本次修改模块，也不调用主 API / FB sidecar。仅在其四个已审查
源码文件与 release 哈希一致时，允许该采集继续运行；实际发布/加工及所有任务租约
仍必须排空。首次排空被独立采集阻塞后已安全退出并恢复状态，未替换运行文件。
新代码开始重新开放后若检查失败，保留代码与账本进入人工检查，禁止自动退回较窄权限。
重新开放前的文件安装失败可安全恢复备份；全程不恢复旧数据库。

准确回退入口为本次 release 中脚本及其返回的 manifest：

```bash
python3 scripts/deploy_fb_page_owner_access.py --rollback /mnt/data-disk/fb-video-publish/backups/page-owner-<timestamp>-<commit>/manifest.json
```

回退会缩小权限，因此存在待执行/未决任务或启用池时拒绝执行，并在排空后再次检查。
先完成相应业务对账和计划处理，保留现库、历史任务和短链；不能为回退清空这些事实。
