# 部署记录与回滚

## 范围
主机43.166.187.96，服务/root/drama_material_service，systemd drama-material-api.service；公共静态/usr/share/nginx/html。代码来自GitHub gaoyuan12356416/ai- 的 codex/meta-drama-asset-delete-20260915 分支。

## 约束
只覆盖本需求文件。切换前逐文件验证真实生产基线SHA，出现漂移立即停止；备份现有代码、双份静态和台账。备份、release、SQLite均位于UUID 3e8ac4e8-7770-456d-9e89-2ec5dd405fa8的数据盘。

## 流程
1. 本地测试/JS语法/编译/diff检查；GitHub提交并验证远端SHA。
2. 服务端从GitHub获取精确提交；数据盘release里执行Python3.9测试与compile。
3. 检查主API及活跃发布任务，保存unit/timer状态；online SQLite backup，备份自校验manifest。
4. 原子替换限定文件，双份静态使用同版本；仅重启主API。
5. 检查服务active、/api/ui/topbar、页面title/资源hash、未登录接口401；使用只读内部校验确认产品目录和ID解析。不会自动提交DELETE。

## 台账
FB_AD_ASSET_DELETE_DB_PATH默认/mnt/data-disk/fb-ad-asset-delete/tasks.sqlite3；FB_AD_ASSET_DELETE_GRAPH_VERSION默认v25.0。不可回退根盘；业务MySQL必须63350且@@read_only=1。旧台账原位置只读展示。

## 已部署版本与证据
- 完成时间：2026-09-15 11:43（Asia/Shanghai）。GitHub分支 `codex/meta-drama-asset-delete-20260915`，部署版本 `9d77fec3fe06492c249db3092cc91b3a496c08e5`。
- 主API与两处静态先部署 `0077d29b3dfb0af505f953c1180ddc3b86606e87`；随后 `9d77fec` 增加 Nginx 转发，应用代码与静态内容相同。
- GitHub服务器检出目录：`/mnt/data-disk/meta-ad-asset-delete/release-9d77fec3fe06`。19个部署目标（包括两处静态和Nginx配置）与该版本逐文件SHA256一致。
- 代码、两处静态、原SQLite在线备份及校验清单：`/mnt/data-disk/meta-ad-asset-delete/backup-20260915T033429Z-0077d29b3dfb`。
- Nginx新增配置 `/etc/nginx/default.d/fb-ad-asset-delete.conf`；变更记录 `/mnt/data-disk/meta-ad-asset-delete/nginx-backup-20260915T0344Z-9d77fec3fe06/plan.json`，变更前该配置不存在。
- 切换时暂停9个发布timer并等待正在运行的发布任务自然结束；仅重启 `drama-material-api.service`。全部9个timer已恢复active。Nginx配置通过 `nginx -t` 后热重载，主API与Nginx均active。
- 公网页面200；`/api/ui/topbar` 200且暴露新模块；新products/jobs接口未登录401。补齐Nginx路由后重新验证，已排除初次公网404。
- 新台账 `PRAGMA integrity_check=ok`，任务数0、执行尝试数0；没有线上真实删除。主API重启后错误日志无新增条目。
- 本地与服务器Python3.9.6均通过106项针对性测试。详见测试报告。

## 精确回滚步骤
仅回滚代码/静态，保留当前V2台账、成功凭据和未知锁；不要把历史台账备份覆盖当前进度。

1. 暂停上述9个发布timer，记录其原状态，等待所有相关发布service自然结束；同时确认本模块没有正在执行的任务。不要强杀发布请求。
2. 执行以下命令。脚本先核验备份及当前部署文件，发现漂移会拒绝覆盖；恢复的旧app会立即关闭三个旧删除入口。

```bash
python3 /mnt/data-disk/meta-ad-asset-delete/release-0077d29b3dfb/scripts/deploy_meta_asset_delete.py rollback /mnt/data-disk/meta-ad-asset-delete/backup-20260915T033429Z-0077d29b3dfb
systemctl restart drama-material-api.service
```

3. 检查主API和健康接口，按备份中的 `timer-states.json` 恢复原本active的timer。保留Nginx转发配置；旧删除入口已在回滚代码中关闭，不能通过恢复旧页面重新执行删除。

9个timer为：`x-auto-post-runner`、`x-auto-post-scheduler`、`x-post-schedule-claim`、`x-post-schedule`、`x-post-manual`、`tt-post-runner`、`tt-auto-post-runner`、`tt-auto-post-scheduler`、`fb-auto-post-runner`（均以 `.timer` 结尾）。

## 生产只读校验与性能边界
- 产品目录329项；826未包含；3543是W2A、父产品3360。
- content_id=66075322仅1个en版本；series_code=XEY271共10版本；content_id=68608322/product3543匹配11个Ad且源归属核查0阻止。
- 指定Ad/Creative真实Meta GET均成功，Creative返回2个视频ID，预览会固定这两个节点。
- 修复MySQL混合字符序错误1267，使用可强制转换的_utf8mb4十六进制字面量；不改变源库结构。
- 源表全局Video引用没有合适索引，实际完整扫描超过180秒。该场景仅Video标为阻止，Creative/Ad仍可处理；不会把查询失败解释为无引用。后续视频处理需全局引用核验完整通过。本次没有新增源库索引或修改业务库。
- Creative经Meta确认账户后按ad_account_id索引核验全部产品，兼容裸ID/act_格式。Video始终单一SELECT包含全部目标，避免多段读取伪装成一致性快照。

## 可执行部署与回滚工具
scripts/deploy_meta_asset_delete.py prepare <GitHub release目录> 生成有校验清单的代码、两处静态和在线SQLite备份；apply <backup目录>再次校验基线后切换选定文件。
rollback <backup目录>保留当前V2台账，恢复代码/静态，同时关闭旧版三个删除入口函数。随后只重启drama-material-api.service。不会重新放开已知不安全的旧删除流程。
