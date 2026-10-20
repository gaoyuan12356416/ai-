# AI 封面入库的窄范围部署与安全回退

本工具只在 CPU `43.166.187.96` 的 root 用户、`VM-0-108-centos` 主机和已确认数据盘 UUID 上执行。`prepare` 只创建备份和组合验证，不能视为已安装；真实执行阶段以服务器 `deployment.json` 为准。正式切换时刻 T 由运维完成旧请求排空后确定。

## GitHub 版本及安装范围

先提交、push、在服务器 fetch 精确 40 位提交，再用 `git archive` 解包到名称为该 SHA 的独立 release 目录。不能从开发工作区直接复制文件。示例变量由执行者填写实际值：

```sh
COMMIT=<已推送并验证的40位提交>
REPO=<服务器已fetch该提交的Git仓库绝对路径>
RELEASE=/mnt/data-disk/deploy/ai-cover-custom-source/releases/$COMMIT
python3 "$RELEASE/ops/ai-cover-custom-source/deploy.py" prepare \
  --repo "$REPO" --release "$RELEASE" --commit "$COMMIT"
```

工具验证提交存在于已获取的 `origin` 远端引用、相关 release 文件逐字节等于该提交的 Git blob，并编译相关 Python 文件。它不会替代执行者对 GitHub push/fetch 的验证。

主 app 是生产组合版本。安装同时校验 `baseline.json` 的完整 app SHA256 和原回调的 `ast.get_source_segment` SHA256；只替换 `post_screenshot_ai_source_callback`，并证明其余 AST 及函数外文本未改变。不把 release 的完整 `app.py` 覆盖到生产。

安装范围固定为：

- `/root/drama_material_service/app.py` 的单个封面回调及 `features/cover_material_source.py`。
- YouTube、日报的 `current` 链接，均指向同一已验证的精确 release。
- 两个服务和 timer 的四个基础 unit 文件；校验 YouTube 对共用账本的写权限、源 SQLite 的只读限制及日报对共用账本的只读限制。

不安装环境变量、凭据或 cron；不启动服务、timer，不调用图片入库接口，不生成封面或发布视频。

## 在线准备及切换前条件

`prepare` 不要求停机。它将以下材料保存到
`/mnt/data-disk/ai-cover-material-source/deployments/<UTC时间>/`：

- app、原共用模块（若存在）、主 API 和两个消费者的基础 unit、合并后的有效 unit 配置及启用/运行状态。
- root crontab、两个原 `current` 链接原文及实际目标。
- 源 SQLite 的在线一致性备份，含已提交 WAL 数据，及 `quick_check`、备份 SHA256。
- 旧 YouTube 同步账本 `/mnt/data-disk/youtube-cover-material-sync/sync.sqlite3` 和共用账本 `/mnt/data-disk/ai-cover-material-source/upload.sqlite3` 的在线备份、原路径、存在性及 SHA256。存在的账本必须成功备份；任何 SQLite/I/O 错误都会使 prepare 失败。尚不存在的账本明确记为 absent，不伪造空库备份。各库分别取得一致性快照，不声称跨库同一事务快照。
- 待安装组合 app、完整 release 文件哈希和 `deployment.json`。备份目录权限为 `0700`，文件默认 `0600`，不会输出配置正文。

执行者独立完成真实样本验证；任何产品限制或未对账结果都不能视为切换通过。随后：

1. 阻止新旧接口继续出站；在共用账本根目录建立 `HOLD`。若当前有新接口请求，按共用 `outbound.lock` 排空后建立 HOLD。
2. 停止 `drama-material-api.service`、YouTube 和日报的 service/timer，确认不存在残留旧 HTTP 请求。工具不替执行者停止生产服务，也不推断 HTTP 已排空。
3. 旧请求全部排空后固定 T，保留实际请求/进程观察证据。历史冻结清单使用同一个 T，不随重跑滚动。
4. 准备以下 JSON 证据，计算其 SHA256。`evidence` 必须指向实际排空记录或说明，不得用这份示例代替验证。

```json
{
  "cutoff": "执行者填写真实带时区的ISO时间",
  "old_outbound_blocked": true,
  "old_http_drained": true,
  "new_outbound_held": true,
  "old_http_inflight": 0,
  "evidence": "实际冻结、排空检查的记录位置和结论"
}
```

`apply` 再次检查完整基线、原 unit/有效配置、root crontab、旧链接和 release 未漂移；要求主 API、YouTube、日报的 service/timer 全部 inactive 或 failed，且 HOLD 存在。它验证证据哈希、时间有时区及显式排空声明；**这些声明来自执行者，工具没有独立证明旧 HTTP 请求已排空**。若冻结流程需要修改 crontab，应在 prepare 前完成，或在修改后重新准备并保留前次备份，不得绕过哈希检查。

## 安装及验收

```sh
python3 "$RELEASE/ops/ai-cover-custom-source/deploy.py" apply \
  --deployment <prepare返回的绝对目录> \
  --cutover-evidence <真实排空证据JSON绝对路径> \
  --evidence-sha256 <该JSON的完整SHA256>
```

所有检查通过后，工具先原子安装共用模块，再原子替换回调，验证其余 AST 不变，然后切换两个精确 release、安装 unit、执行 `systemctl daemon-reload`。只在这些操作全部验证成功时记录 `phase=installed_held`。HOLD 保留，服务继续停止；`installed_held` 不是接口或历史补传成功。

由执行者分别完成：

- 按已冻结清单及账本检查新旧接口对账，确认真实产品、`content_id`、图片类型和人员归属。
- 以新代码窄启动主 API，在 HOLD 下确认健康、非封面功能和已有制作 worker 的状态；不进行无关服务重启。
- 明确解除 HOLD 后，恢复两个消费者原有调度并验证新请求、逐图回执、日报迁移排除和断点恢复行为。
- 保存最终进程/服务状态、Git SHA、切换 T、清单哈希及新旧素材 ID 对照。补传未全部对账前不得报告完成。

`apply` 中途失败时，`deployment.json` 保留 `applying` 或 `bridge_installed` 阶段。不要重跑到新的 T、覆盖原备份或直接恢复旧 callback；先读取实际安装状态并保持 HOLD。

## 安全回退

```sh
python3 "$RELEASE/ops/ai-cover-custom-source/deploy.py" rollback \
  --deployment <本次prepare返回的绝对目录>
```

这里的回退是**停止继续出站**：在共用锁下持久化 HOLD，禁用并停止 YouTube timer，停止其 worker，验证 app 仍保留新入库桥接。不会恢复旧 callback、切回旧 YouTube 发送器、恢复数据库或修改已入库素材。新旧账本、视频发布状态、日报历史、精确 release 链接和桥接代码全部保留。

如果检测到桥接漂移，工具会在 HOLD 和 YouTube 停止之后报错，要求按实际状态处理。任何后续代码回退都必须保留“已尝试新接口的图片版本不能进入旧接口”保护；源库、旧 YouTube 同步账本、共用上传账本的三类备份均仅用于取证，禁止覆盖运行库。

验证命令：

```sh
python3 ops/ai-cover-custom-source/test_deploy.py
```

测试只使用临时文件、临时 SQLite 和 mock Git/systemd。Windows 本地的链接切换及 Linux `flock` 由 mock 隔离；上线前仍需在目标 Linux 主机验证 `systemd-analyze verify`、路径权限及实际链接切换。
