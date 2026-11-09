# X Excel 免费集 ID

Excel 的「素材 ID」列现在可混填普通数字素材 ID 和 32 位十六进制免费集资源 ID，表头与其他列不变。资源 ID 按文本读取并统一小写。示例表包含两种格式，虚构 ID 必须替换为真实 ID。

免费集按 `ads_drama_resource.id` 精确查询，核对 Dramawave 视频、免费范围、语言、投放时间及媒体元数据。不存在、非免费集、无可用视频等错误逐行展示，其余有效行可继续提交。发布仍使用个人 Excel 的账号、文案和北京时间；不建立剧池绑定或修改剧池进度。

主 API 的预览/提交查询为整批免费集提供 8 秒元数据预算。仅时长尚未读取时返回 `duration_status=pending` 和空时长，页面明确提示待执行前校验；身份、免费范围、域名、投放时间失败不能降级。执行器每个子任务仍严格读取实际视频时长，并在创建队列前按当前账号复验会员要求，之后走原有完整媒体验证。数字素材规则不变。

保留数字素材和旧接口的 ID 规则。仅个人文案合同 v2 任务可以将资源 ID 存入手动队列。预览、提交、执行前核对同一资源、集数、剧目与源视频地址；新队列以及存储重启均支持资源 ID。现有占用、未知结果及有效预约保护覆盖同资源的剧池和 Excel 路径。

验证：真实 SheetJS `.xlsx` 解析测试，以及精确源查询、混合输入、免费范围、语言、视频时长、预览至队列、存储重启、跨入口防重和有效预约测试。测试不调用 X 发帖。

## 部署与回滚

先将代码提交 GitHub。服务器检出该精确提交后运行：

```sh
python3 scripts/deploy_x_excel_episode.py --phase prepare
python3 scripts/deploy_x_excel_episode.py --phase drain --folder BACKUP
python3 scripts/deploy_x_excel_episode.py --phase status --folder BACKUP
python3 scripts/deploy_x_excel_episode.py --phase switch --folder BACKUP
```

准备阶段复制当前 X 组合版本，仅覆盖本次文件；主 API 的独立 `service.py` 用本次差异精确应用，保留其他功能。当前源文件 hash 变化时需重新核对。备份与版本均位于 `/mnt/data-disk/x-post-automation/`。

主 API 的时长读取配置增加已被现有 X 发布器信任的 `img.tianmai.cn`，保留其原域名列表。该非敏感配置通过独立 EnvironmentFile 安装，随代码备份和恢复。

切换只排空 X 及主 API 自身截图线程，其他频道和独立工作进程继续运行。主 API 来源查询先就绪，再切换 X，按原状态恢复 X 调度器，并读取调度健康报告。不得终止封面生成、清租约或重置队列以完成部署。

尚未切换时可用 `--phase abort --folder BACKUP` 恢复 X 原入口。未产生任何免费集任务时，完整代码回滚使用 `--phase rollback --folder BACKUP`，等待排空后执行 `--phase switch --folder BACKUP`。保留当前数据库、凭据和平台回执。

一旦已产生免费集历史任务，旧运行时无法读取这些新 ID；此时应只恢复原 Excel 静态入口并保留新 ID 读取兼容代码，或发布兼容回滚修订。不能恢复旧数据库或直接切回不兼容运行时。
