# 剧集合成升级：上线与真实发布验收

## 当前结论（2026-08-27 17:31，北京时间）

CPU/HK 主体部署完成，Shahrul Ikmal 的唯一非公开测试已成功：一次上传、一条评论，ads_ai 三张新表各一条，三个本地同步事件全部 synced。外部新鲜读回确认 unlisted / processed / succeeded、标题描述、评论内容及作者均匹配。同任务完成后再次执行，4SQLite表/3MySQL新表行数及全行hash均未变。正式 live/sync 暂为 0，频道加载提示修正完成后再放行；此时尚不宣称全部发布工作结束。

现行方案复用 CPU 现有 ads_aius 数据库凭据及已获取的 YouTube OAuth，不创建数据库账号、不执行 CREATE USER / GRANT / ALTER USER、不写原 MySQL 表。刷新得到的 access token 只在 CPU 进程内使用，不回写原授权表。专用账号/1410 阻断已按用户要求取消，不再要求管理员提供配置。

## 已完成的需求与职责

| 项目 | 已取得的证据 |
| --- | --- |
| 页面风格、默认输出与下拉 | 复用原页面布局/CSS；线上郜远会话验证四个输出项默认未选，封面模板及命名规则下拉移除，20 个历史任务保留 |
| 随机模板 | CPU 本地固定 SHA manifest 提供 FB-v3 四层 3/5/3/7，即315组合；线上自动/手动选项可用；HK 自动/手动真实成片与重放沿用未变 e1f5a1d 的独立媒体验收 |
| 计算职责 | CPU 业务查询、OAuth、发布、记录与短链；HK 只接受冻结制作输入、制作、上传 COS 并回传；正式制作地址18788，正式 COS 前缀drama-materials |
| 任务列表操作 | 已完成行直接提供复制素材 URL、生成短链、发布到 YouTube；保留详情入口和既有按钮样式；多产物选择已实测 |
| 短链 | 同一任务/合集视频只生成 id=1，线上列表再次生成复用该链接；HTTPS 200与wrapper SHA一致，未使用page.dramabuzzs.com |
| YouTube | 频道弹窗列出实际可用频道；选择Shahrul后评论可编辑；未点击正式 public 入队按钮，真实测试走固定的受控 unlisted canary |
| 发布记录 | ads_ai.ads_youtube_videos / ads_youtube_comments / ads_youtube_publish_log 各1，opaque用户892fd2e8，完整payload与SQLite outbox一致；原账号Token/client指纹前后不变 |

## 唯一真实测试

- operation：`drama-hk-deploy-unlisted-20260827-shahrul-263`；本地发布任务：`1`。
- 产品1479，频道263，账户255；频道：`Shahrul Ikmal / UCHJ1jFaYuW8g5EM7hM5pPpg`。
- 操作人ID：`892fd2e8`，已核验对应登录用户郜远；内测账本operator_name按CLI固定为`internal-deployment-canary`，不是把存储姓名写成郜远。原制作任务：`0af9842430274e0cbb55fa852cc474e6`，原合集视频88,913,963 bytes / ffprobe267.909秒。
- 视频：[HGgjhhRXS-I](https://www.youtube.com/watch?v=HGgjhhRXS-I)，非公开；评论：`Ugwktiv9_nnXb1TN_c54AaABAg`。
- 短链：[https://gy.g2flow.com/s2l/youtube/1.html](https://gy.g2flow.com/s2l/youtube/1.html)。目标为`https://www.dramawavew2a.com/ads/101/2284/view?af_dp=ApWdS7hDnF&c=ai_youtube&af_channel=ai_youtube&af_c_id=0af9842430274e0cbb55fa852cc474e6`。
- 标题：`DramaWave deployment verification - unlisted canary 20260827`。
- 描述模板：`Internal deployment verification. Test video remains unlisted.\n{{url}}`，实际描述占位符已替换为上述短链。
- 评论：`DramaWave deployment verification: one unlisted canary, no public release.`
- 17:21:21 上传完成；同ID在17:22:19核对为processing；17:24:50视频/评论published且同步完成；17:26:50再次从YouTube读回processed/succeeded/unlisted、4分28秒和唯一评论。
- 初次transient unit启动因systemd239把`TimeoutStartSec=0`解释为立即超时而退出，租约generation与两项attempt均0；修正为`TimeoutStartSec=infinity`及`--no-block`后才发生唯一上传。没有因启动错误重复发布。

## 版本、测试和证据

CPU基础代码为GitHub精确提交`59f95e6dc106a420fa2e326597c931ba712249f9`；OAuth HEX与列表入口修正部署为`ee6e00c000c31a538b9294a9da7f084dd9e5f9ac`。writer代码仍为未变59f95e6，HK为未变`e1f5a1d04cfb510df9c2444ac592adec2827508b`。组件版本不能改绑最终文档提交。

一次完整272/272回归只属于59f95e6；ee6增量为独立119/119定向、Node16检查、独立内存6/6与10场景20处理器调用。未重复整套、未把不同阶段数字相加。HK真实媒体报告SHA为`40746316694eeb4d34fb4511713acb13b8de14cff382f6116fdd99f1351f2175`，本次DB/UI修正未重新制作或覆盖这些成片。

CPU私有机器证据根目录：`/mnt/data-disk/drama-youtube-ads-ai-deploy-20260827/ee6e00c000c31a538b9294a9da7f084dd9e5f9ac/`。

- `cpu-hex-credential-preflight.json`、`cpu-fix-cutover.json`：实际CPU读取、5文件哈希、3服务重启及writer PID守恒。
- `canary-frozen-preflight.json`：测试冻结字段、短链参数/HTTP/wrapper SHA；发布前新三表各0。
- `canary-run1b-dispatch.json`、`canary-run2-readback.json`、`canary-run3-readback.json`：唯一上传、仅核对既有video ID、最终状态。
- `canary-final-external-readback.json`：新鲜外部状态/文案/作者、三表完整载荷哈希、3个outbox各attempt1、原授权指纹守恒。
- `canary-idempotency-readback.json`：完成后同ID重放，外部身份/次数不变，4SQLite表与3MySQL新表计数及全行hash均不变。
- `release-invariants.json`：17:36补充实际冻结字段、ads_ai/连接端63353/ads_aius身份、三个完整payload与对应outbox、15+5备份成员和两份SQLite逐SHA核验、组件文件与clean release比对；不修改前四份历史报告。SHA为`7c7a4dd126da1f26bbe4541efa2efa491a2c62df6896f6672496ca5c3d5eb3e4`。

## 备份与停止/回滚边界

升级前CPU备份：`/mnt/data-disk/drama-synthesis-cpu/backups/20260827T1630-pre-shared-account`；ee6前备份：`/mnt/data-disk/drama-synthesis-cpu/backups/20260827T091634Z-before-ee6-fix`，manifest SHA`180b137af3c08a101a3956d220fc7183f73c3279789e8e61e324f9942404cc36`。

HK正式前缀配置备份：`/data/drama-synthesis-gpu/backups/20260827T1650-pre-formal-prefix`；1650仅目录标签，实际激活在16:42左右。旧X服务未被重启。所有操作通过SSH，未进入腾讯云管理后台。

发生问题先关闭live/sync并停止新增领取，再核查在途租约和外部身份；unknown不自动重传/重评。源码可按精确备份回滚，但**不能用发布前SQLite覆盖已发生的外部事实**。保留新MySQL三表、SQLite/outbox、短链、COS与本测试视频/评论，不DROP、不删外部记录，不盲目退旧缓存版本。正式UI仍按附件使用public；内部测试unlisted不代表正式功能默认隐私改变。
