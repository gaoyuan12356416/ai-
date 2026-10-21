# 部署文档

## 变更内容与基线

Page 管理新增 Excel 预检、原子批量发布、操作恢复与下载模板；素材池改为 ID 分行输入。原协议、历史任务、发布回执与未知结果防重保持兼容。

分支 `codex/fb-page-excel-publish-20261021` 从 `3e6b75e` 创建。2026-10-21 已逐一读取线上目标文件 SHA256，与该基线的 Git blob 一致。CPU 当前业务部署基线包含 `c926ef6` 的 Token 轮换修复。主工作区有用户修改，本次在独立 worktree 开发。

## 配置与部署范围

- `FB_VIDEO_EXCEL_ENABLED=1` 仅开放 Excel 新预检/提交；关闭后仍可查询已受理操作，执行和对账已冻结任务。
- drop-in：`/etc/systemd/system/fb-auto-post-service.service.d/zz-fb-video-excel.conf`，来源 `deploy/fb-video-excel.conf`。
- 原发布开关、加工开关、每日额度继续生效，未改变 GPU 容量。
- `features/fb_video_publish` 修改同步 `/root/drama_material_service` 和 `/opt/fb-auto-post/current`；`features/fb_auto_posts/{service,repositories,publisher}.py` 只同步 FB runtime。主 API 的旧 publisher 等副本不同且不执行此发布链，不覆盖。
- 7 个静态资源同步 MAIN/static、FB/static、`/usr/share/nginx/html`；三页缓存版本同步更新。
- 无数据库迁移，新协议字段保存在原请求和任务 JSON 快照内。服务机已有 openpyxl。

## GitHub-first 部署步骤

1. 完成隔离 SQLite、模拟平台接口和浏览器 fixture 验证；提交并推送上述分支。
2. CPU 43.166.187.96 从 GitHub 拉取并检出精确 commit 到 `/mnt/data-disk/fb-video-publish/releases/<commit前7位>`。
3. 在检出目录运行 `python3 scripts/deploy_fb_excel_publish.py`，验证 GitHub ref、目标基线/新文件缺失、挂载 UUID、空间、池计划窗口和在途工作。
4. 运行同脚本 `--apply`。脚本记录原服务/timer 状态、逐文件备份和 manifest；停止原本活动的生产者，排空后备份 SQLite，再核对哈希并安装。离启用池计划触发点过近则退出，不跨越计划窗口。
5. 仅重启受影响的主 API 与 FB sidecar，恢复 worker、池调度和 timers 原状态，核验健康和全部安装哈希。不得启动原本停用的 timer。

## 上线验证

- 发布目录先运行新增 Excel/个人凭证/素材 ID 测试；部署后核对实际安装模块与精确 commit。
- 公网 Page 管理、带缓存版本 JS、下载模板可读；未登录 API 仍需登录，私有 broker 不对公网开放。
- 服务/timer 状态、SQLite integrity/模块原有事实、短链文件哈希与备份/账本核对。
- 使用只读真实来源查询验证素材 ID 批量解析和个人授权映射；验收不提交真实 Facebook 帖文，不创建生产测试任务或短链。
- 精确提交、备份路径、部署结果和线上证据在 `test-report.md` 追加。

## 回滚

在 CPU 对本次 manifest 运行：

```bash
python3 /mnt/data-disk/fb-video-publish/releases/<release>/scripts/deploy_fb_excel_publish.py --rollback /mnt/data-disk/fb-video-publish/backups/<backup>/manifest.json
```

这是功能回滚：关闭 Excel 新入口，恢复旧静态，始终保留兼容 Python 执行与对账代码。已受理 v3 或 unknown 任务不能交给旧 worker。操作查询继续可用；原池计划与旧发布入口继续按既有规则运行。

仅当应用失败发生在首次服务启动尝试前、已完全排空、且查库确认没有任何已受理 v3 批次时，部署器才允许按备份恢复全部代码。首次启动尝试先记录到 manifest；之后失败也按功能回滚处理，不能假设没有任务已受理。外部并发启动也由查库兜底；查库不确定或文件未完整兼容则停止待核对，不能退回旧 worker。

任何回滚均不恢复旧数据库覆盖现库、不清空任务、不重试历史失败、不删除短链。哈希发生第三方漂移则停止，并依据 manifest 核对。旧版本部署器不能用于绕过上述保护。
