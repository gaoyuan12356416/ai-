# 部署文档

## 变更内容

FB 未决发布按 Page + material 隔离；保留真实写入 Page 串行和同素材防重。部署入口为 `scripts/deploy_fb_page_task_isolation.py`。

## 配置项

不增加配置。保留 live 发布开关、当前渠道容量及全部原预约。临时关闭 FB 调度领取，逐项记录并恢复 9 个常规 FB timer 的原 active 状态；不触碰历史一次性 timer。

## 数据库变更

事务中替换 manual/auto 的 Page execution partial UNIQUE 与跨 family triggers；仅 running 占有 Page 写锁。原数据库、short link、attempt、claim、operation 全部保留。中央 collector 根据可信 native snapshot 把历史 Page unknown 锁迁为素材锁。

## 部署步骤

1. 提交并推送 GitHub；服务器 fetch 精确 SHA，在单独干净 worktree 取码，不改共享 checkout。
2. prepare 从当前 FB 与 coordinator release 复制候选，逐文件比较已审基线 dce503c，再 overlay 白名单差异。其它平台运行时代码按原样保留。
3. Linux 定向回归，在线 SQLite 备份，候选构造器在副本重复迁移并 quick_check。
4. apply 暂停 FB 新领取及常规 timers；等待原生在途写入/准备结束。按端口临时停止新 HTTP 请求，已接受请求自然排空后单独重启 FB sidecar、coordinator。临时规则附带 watchdog；主 API 与节点不重启。
5. 健康检查通过后恢复原 FB 配置及 timers；核对运行文件哈希、索引、资源锁、旧发布回执和 Excel skipped 历史。

长耗时纯读取规划可显式使用 `--defer-plan-read`。脚本必须同时证明唯一 HTTP client 为 systemd 的 runner plan 模式、外连仅只读 MySQL、无媒体/发布任务、唯一原 due 尚无 run 且预约在 1 小时以后。停止旧进程后以原 due id/owner/lease CAS 延后；若竞态中已创建 run，则只完成原 due。禁止对 Graph、GPU 或其他 handler 使用此分支。

2026-11-04 实际部署提交 `ba6e31d31ffb0f9f24df82fbdf9213bed4445ce0`。运行候选与审计位于 `/mnt/data-disk/pipeline-dispatch-runtime/fb-isolation-ba6e31d31ffb`；FB gate 0.256 秒，coordinator gate 15.031 秒；主 API 未重启。私有维护目录不具服务用户遍历权限，候选必须放可执行的 runtime 根目录，不能直接链接私有维护目录内的候选。

## 验证步骤

本地与 Linux 单测见 test-report.md。生产验证只读，不创建测试帖子。被跳过的 15 行由用户重新上传原 Excel，不补建任务。

## 回滚方案

不得恢复部署前整库或把已发任务改回 queued。新 schema 允许同 Page 多条 submitted/unknown，旧构造器会重建冲突索引，因此必须保留兼容迁移/索引代码，使用新的精确 GitHub 修复提交前进恢复。partial apply 会保留 FB 暂停，待修复后精确恢复原状态；不能假称所有步骤完成。

## 注意事项

已知 Graph ID 只 GET 核对；无结果的历史任务不自动重发。无可信素材身份的历史行仍保守留 Page hold，不推测素材、不消费无关池条目。审计与备份留数据盘，外部凭证不入 Git/log。
