# FB 短链辅助服务防复发

2026-11-03 停主 sidecar 后，恢复清单遗漏短链常驻 worker，造成后续批次等待短链。本次保留原任务、预约、回执和防重规则，补齐部署恢复与持续观察。

## 生效措施

1. `publish_scheduler_health.py` 部署快照覆盖服务、timer/path 及实际 Requires 依赖者；在成功、异常和代码回滚路径核对恢复。只启动部署前运行、当前停止的常驻单元，不直接执行 oneshot，不重启仍运行的服务。不把部署前主动暂停的服务恢复为运行。
2. 沿用已部署 `94-page-pool-support.conf` 的弱依赖 `Wants=fb-video-publish-worker.service`。保留 worker `After=fb-auto-post-service.service`；worker 失败不阻断主服务。部署切换用限定范围的恢复逻辑，常规主服务启动仍带起 worker。
3. 每分钟运行独立只读健康检查。管理员“制作与发布监测”显示服务状态和短链积压；同时记录 journal，异常时检查服务返回失败。仅将到期、素材 ready、无核对占用且持续等待短链 300 秒的任务告警。未来预约、平台处理中、发布执行中和未知结果分别显示。观察中断超过 150 秒显示未知并重新累计，缺失数据不按零算。告警不修改任务、租约或预约，不自动重发。

## 标准部署入口

GitHub 精确提交部署到 `/opt/publish-scheduler-health/current`。新版本 `scripts/deploy_publish_queue_isolation.py` 的 FB 正向及回滚入口已内置快照/恢复；`deploy_final_queue_entries.py` 调用同一入口。

其他需要切换 FB 服务的发布流程，使用固定入口包裹**已审查、按 GitHub 提交拉取的部署命令**：

```sh
python3 /opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py run \
  --lane fb --audit-dir /mnt/data-disk/DEPLOYMENT/support -- DEPLOY_COMMAND
```

范围内服务切换仍须排空自身 HTTP/执行阶段；该包装器不提供中断在途工作的许可。常驻服务启动使用 `systemctl --job-mode=ignore-dependencies start UNIT`，由快照恢复已运行的依赖者，避免启动维护前主动暂停的 helper。不能使用历史部署脚本绕过这一步，也不应复用旧快照恢复当前服务。

## 部署与回滚

`deploy_fb_publish_reliability.py --source GITHUB_ARCHIVE --commit FULL_SHA` 校验 Git 对象和源文件；基于当前线上 monitor 做最小补丁，保留已有统计范围和性能修复。只切换已排空 HTTP 请求的 coordinator；不停止 FB 主服务、短链 worker、节点或 API 主服务。

备份目录 `/mnt/data-disk/fb-publish-reliability-20261106/deployment-TIMESTAMP/` 包含原代码指针、静态文件、定时器、SQLite 在线备份、服务快照、隔离演练与 HTTP 排空证据。执行该目录 `rollback.sh` 回滚代码、静态文件、观察定时器和健康工具指针。保留当前数据库、回执、租约、凭据和业务事实；不恢复数据库备份。已存在的弱启动依赖保持原样。

## 验证

```sh
python3 -m unittest scripts.test_publish_scheduler_health scripts.test_fb_publish_reliability
node scripts/test_fb_support_ui.js
python3 scripts/test_fb_support_systemd.py
```

最后一项仅创建并清理 `codex-fb-support-proof-*` 临时单元，演练弱依赖失败、主服务停服传播、成功/失败恢复、暂停保留与 oneshot 不重放。不会停止业务服务。页面另以本地假数据检查正常和异常显示。

线上验收：确认定时检查至少连续两次更新，服务 PID 保持，监测接口权限不变；比对原 37 条任务冻结字段、15 条平台成功回执和 22 条未来预约，无新增重复操作。
