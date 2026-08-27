# 测试报告

## HK 缓存增量独立复验（2026-08-27）

在 CPU 候选 c719beb 之后新增的 HK 缓存修复已独立 QA 通过，当前仅允许提交并部署新的 HK 隔离 release；CPU 候选和三表证据仍绑定 c719beb，不转移到新 SHA。真实 v3 fresh/replay 与服务重启重放尚待执行，不据代码回归宣称已完成媒体最终验收。

- 六套合并共 188 项，首次 187 PASS、1 项因迁移文档缺少实际错误字符串失败；补回说明后该项定向复测 PASS。没有重复整套，不称“一次全绿”。此统计包含下文 166 基础用例，不相加。
- 27 文件语法及 Python 3.9 AST、diff 检查通过；5 个缓存增量冻结代码文件测试前后 SHA 一致。
- 新版本清单校验失败、HEAD 异常和配方冲突不回退重制；独立发现的缺失 profile P2 已修复，缺失/错误 profile 在 HEAD 之前拒绝。见 BUG-020、BUG-021。

## 2026-08-27 最新结论

代码候选 `c719bebf72be900ec3853858dc53b36b83beffd2` 已完成独立 QA，GitHub push/readback 已由发布主代理确认。代码 QA PASS，整体 production release 仍为 HOLD；不能把代码缺陷修复、隔离环境准备或表级演练等同于正式发布完成。

用户当前授权：所有支持与服务器操作仅通过 SSH；环境门禁通过后继续部署，并只在 **Shahrul Ikmal** 执行一次内部 `unlisted` 视频测试及一条评论。禁止进入/操作腾讯云管理后台，不允许 public 测试，不修改现有 X/`ads_video_producer` 业务。本轮文档更新没有服务器操作。

频道冻结为 app `1479` / local channel `263` / account `255` / YouTube channel `UCHJ1jFaYuW8g5EM7hM5pPpg`；唯一 operation 为 `drama-hk-deploy-unlisted-20260827-shahrul-263`。正式 HTTP/UI 仍固定 public，不能传入 canary 参数绕过正式开关；内部 CLI 的 live/sync 开关均保持 `0`。

### 唯一合并回归统计

| 验证 | 本轮结果 | 证据边界 |
| --- | --- | --- |
| 五个 Python suite 一次合并执行 | 166/166 PASS，12.151 秒 | 独立 QA；是本轮唯一 unittest 总数 |
| 25 文件语法及 Python 3.9 AST | PASS | 语法兼容检查，不替代 CPU/HK 运行验收 |
| 冻结范围与 diff | 12 个冻结文件测试前后 SHA 一致；`git diff --check` PASS | 文档后续更新不改变已测代码 |
| 独立媒体对抗 | 另 5 项 PASS | 内存 mock，不叠加到 166，也不算真实 FFmpeg/CUDA/COS 验收 |

独立 QA 的实际合并命令：

```powershell
python -B -m unittest scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_upgrade scripts.test_drama_youtube_unified_rpc scripts.test_drama_youtube_canary scripts.test_drama_youtube_three_table_rehearsal -v
```

5 项媒体对抗分别为：片头丢失（5.021016→3.966667 秒）、音频补齐掩盖视频截断、长片少 1 秒（7200→7199）、视频流时长 NaN，以及正常舍入对照（5.021016→5.000000）。前四项均拒绝坏产物、清理失败输出、不计算最终 SHA、不进入上传；对照正常返回。FFmpeg、ffprobe、文件写入及上传均为 mock；证据保留在独立 QA 任务工具输出，未生成单独落盘报告。

### 本轮已验证的代码行为

- 固定频道、单 operation、真实已完成 job/source 绑定，普通 worker/outbox 与 canary 隔离；重复操作员、重复 CLI 不创建第二个上传会话或评论。
- 上传前持久化 session intent；未知结果只对账原会话/视频；没有身份的未知上传与未知评论阻断，不盲重试。
- 公共和 canary 评论均传入冻结 `channelId`，只接受响应 `snippet.topLevelComment.id`；2xx 但缺失/身份不匹配仍为 unknown。依据 [commentThreads.insert 官方合同](https://developers.google.com/youtube/v3/docs/commentThreads/insert)。
- 任何 claim/OAuth refresh/upload 之前，先验证鉴权 RPC health 的固定 writer 身份、主库可写、精确 schema/index/grants。仅配置 executor 不算健康通过。
- 每条 canary outbox claim 之前重新读回 processed/succeeded/unlisted；包括已完成任务遗留的 pending 重试。读取失败或隐私漂移会持久化 hold，0 新 outbox claim，不重发已确认评论。
- 三表快照/恢复脚本的端点、容器、schema、数据盘、凭据权限、候选及文件 SHA、数据/结构/索引不变量与证据时效均有离线回归。

### 真实环境状态（截至本次文档更新）

| 项目 | 已确认 | 尚未完成 |
| --- | --- | --- |
| HK dark 环境 | 最新 `c719bebf…ffd2` 已运行；auto 模式真实 concat/no-BGM/random、3 个 COS 文件下载、5 秒/150 帧及解码通过 | 同 job、同 payload 第二次 POST 重复制作，幂等断言 FAIL；缓存窄修已本地完成，独立合并回归、增量发布及完整双模式/重试实测仍待完成 |
| Demucs 真实 CUDA 专项 | 专用用户、mdx_extra_q 四模型：1 秒静音输出 44100 帧/peak 0；2 秒反相立体声输出 88200 帧/peak 0.0079345703125，均 finite；非假模型 | 此专项不代替整条媒体路径的幂等与双模式验收 |
| CPU 主应用 | 仍保持旧 `18787`，未切流；新 `18788` 为隔离路径 | 主应用发布/切流和业务读回 |
| 三表数据保护 | SSH 真实一致性 snapshot + CPU 本机隔离 MySQL `5.7.44` 恢复/迁移演练 PASS；299309 行 | 该结果仅证明本次三表范围，不能替代合法生产迁移账号，也不是全集群灾备；详情见 [迁移文档](migration.md) |
| 生产数据库/RPC | 当前 `ads_aius` 对 `kunlunads_dev` 只有 SELECT/SHOW VIEW | 无合法 admin/migrator/writer，阻塞生产 DDL、健康 RPC 和真正 YouTube 测试；不得复用只读账号绕过 |
| 指定频道测试 | 已授权且只读唯一定位 | 0 真实上传/评论；待全部门禁通过后精确 CLI canary，最终 processed/unlisted、评论和三表读回仍待验 |

主代理经 SSH 完成的真实快照目录为 `/mnt/data-disk/drama-youtube-rehearsal-20260827a11c0001/snapshot`，绑定候选 `c719bebf72be900ec3853858dc53b36b83beffd2`；视频/评论/日志分别为 244151/53/55105 行。机器生成证据如下：

- `snapshot-manifest.json` SHA-256：`426685eda5041d332cde8f70ca724a7bbc3ae6038a0da6d02d1fabc2233f0603`。
- `rehearsal-result.json` SHA-256：`0178a8b633c6433cffca4be32cdb4b5adfaa47e63bcaafb1398d847455d7d43b`。
- `backup-evidence.json` SHA-256：`36579d5ed7a2234d821638b3644c4b32ce024354cbdc136aa97b53dbc3fe9dec`。

证据类型是 `table_snapshot_rehearsal`，不是 Tencent API 云备份或 CynosDB 全集群灾备证明。后续主代理将在 [HK 实测记录](hk-gpu-setup-20260827.md) 及本报告追加幂等修复与最终状态。

### 缺陷与发布建议

BUG-013/014/016/017 与 canary 两项 P1（BUG-018/019）的代码修复已通过独立复验；BUG-015 已有上述真实静音/反相 CUDA 专项证据。具体环境验收不能因缺陷代码关闭而自动记 PASS，见 [缺陷索引](bugs/README.md)。独立离线 QA 冻结时未发现残留 P0/P1；后续真机新增的重复 POST 幂等失败由主代理维护 BUG-020，不能据旧 QA 宣称当前无集成缺陷。

缓存增量目前仅完成本地修复与开发者 focused 21/21；不并入上面的 c719beb 独立 166 例，不代表已推送/部署或真机修复通过。独立合并回归、HK 新 SHA 与最终实测由主代理后续追加；CPU 候选和三表演练证据仍绑定 c719beb。

可继续授权范围内的隔离实测；已有三表证据在 apply 前须重新核对 SHA/时效，不能重复覆盖已完成演练目录。不得提前打开正式 live/sync、切 CPU 流量或执行 public 测试。真实 canary 的授权已经具备，当前阻塞是前置环境/权限门禁，不是“用户尚未授权”。

## 历史：2026-08-26 Wave8 与线上实查增量

以下为旧候选 `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883`、`2b26b540660fd3687fa7c66e68a246d1a706136a` 的历史证据。它们不包含次日发现的运行包、媒体时间轴、canary 与恢复演练缺陷；旧授权边界已由上文取代。所有历史测试数均不得与 166 相加，也不能作为最新 SHA 的重新验收。

### 当日独立 QA 证据

2026-08-26 对 exact SHA 执行：

- focused 45/45 PASS；broad 77/77 PASS；实际 Chrome Playwright 3/3 PASS。
- compile 11/11 PASS；Python 3.9 AST 11/11 PASS；browser spec syntax 1/1 PASS；inline JS 4/4 PASS。
- unified writer：3 个正常实体合同 + 26 个 adversarial 合同用例全部 PASS；outbox malformed/fencing 9/9 PASS。
- hostile recipe 的 img/onerror/script/quotes 以文本可见，0 执行、0 DOM 注入；两 UI mirror 一致。
- 未发现 candidate P0/P1。旧候选 `f05e10f`、`2df9aef`、`d27c82c` 均为 HOLD/obsolete，不可替代 Wave8 SHA 作为发布候选。

### 当日实现者补充证据

- focused 45/45 PASS；相关 broad 116 collected：115 PASS、1 个 Windows POSIX permission 预期 skip、0 failure；实际 Chrome Playwright 3/3 PASS。
- 本地 py_compile 10 个文件、HK Python 3.9.6 stdin-only runtime compile 9 个文件、browser spec `node --check`、两 HTML 6 个 script block parse、static mirror、staged diff/secret/scope/artifact 检查均 PASS。
- 全部外部动作使用 temp/fake；未执行真实短链 writer、YouTube 上传/评论、统一 MySQL 写入、CPU/HK 部署。

### 当日发布结论（已被 2026-08-27 更新取代）

当日 Wave8 与线上实查后的代码增量通过独立 QA，但并非 production release PASS。gy `/s2l/youtube` app-owned root、Nginx 隔离路由和 X 兼容性检查完成；统一三表迁移、账号/RPC 与固定 public 合规风险留待后续。当日尚未取得真实 YouTube 精确授权；2026-08-27 已取得上述指定频道一次 unlisted 测试授权，不再沿用该旧判断。

### 当日线上实查后的增量证据

- X 渠道现行机制已核对：先在 SQLite `x_post_publish_log` 预留自增 ID，以该 ID 生成 `https://gy.g2flow.com/s2l/<id>.html`，冻结 long/short URL 和正文，再原子创建不可覆盖 wrapper，成功后才进入 X 发布；抽样 ID `633` 的数据库 long URL、数字文件名与 HTML canonical 一致，现有短链返回 200。
- YouTube 不创建新域名、DNS、证书或 server block；只复用现有 `gy.g2flow.com`，增加优先级更高的 `/s2l/youtube/<数字短码>.html` 隔离路径。CPU 已建立 `drama-youtube` owner/root 与 Nginx snippet；`nginx -t` PASS，X `/s2l/633.html` 仍为 200，不存在的 YouTube 数字路径为 404，POST 为 403。未生成真实 YouTube 短链文件。
- 统一三表确认已存在于 `kunlunads_dev`：`ads_youtube_videos`、`ads_youtube_comments`、`ads_youtube_publish_log`。当前应用账号只读；增量实现提供固定白名单 loopback RPC、独立 0600 DB/RPC 凭据、三表完整 legacy 字段映射、负数 synthetic queue join，以及 external-id nullable 列/唯一索引的可审计迁移脚本。
- 首次增量独立评审结论为 HOLD：发现 writer 18836 与现有 FB 隧道硬冲突，以及 migrator/runtime 权限、精确 schema/grant、credential owner/0600、ACL 与共享库回滚合同缺口。该结论阻止了提交和部署。
- 修复候选改用经 CPU `ss`、线上配置和仓库三方核验为空闲的 18837；新增可复现 writer env、一次性 migrator、长期最小权限 writer、全量 schema/grant fingerprint、fresh backup evidence/rehearsal、exact owner/0600、短链 ACL 检查和安全回滚。实现者 Python unittest 91/91 PASS；实际 Chrome Playwright 3/3 PASS；CPU Python 3.9.6 对七个运行文件 compile PASS；线上只读 45 个 legacy 列 fingerprint 与 ACL `--check` PASS；`git diff --check`、changed-file secret scan 0 PASS。
- 线上 MySQL 已只读确认是 `5.7.18-cynos-2.1.14-log`、`@@read_only=1`、账号 host 为 `43.166.187.96`、`information_schema.ROUTINE_PRIVILEGES` 不存在、`SHOW GRANTS` 使用单引号账号。候选因此不查询不存在的表；USER/SCHEMA/TABLE/COLUMN 由 information_schema 精确闭包，routine/proxy/未知授权由 `SHOW GRANTS` 白名单拒绝。
- 第四轮最终提交前独立复审 PASS，P0/P1/P2=0/0/0；focused 46/46、RPC/migration 7/7、MySQL57 grant matrix 8/8、related broad 115 PASS + 1 预期 skip、CPU Python 3.9.6 compile、diff/secret/artifact 全部 PASS。随后对 immutable code SHA `2b26b540660fd3687fa7c66e68a246d1a706136a` 再跑实现者 unittest 91/91、Playwright 3/3、CPU compile 7/7，全部 PASS。
- 上述代码 QA PASS 允许进入生产门禁执行，不等于 production release PASS，也不授权真实短链、MySQL DDL/写入或 YouTube 发布。
