# 测试报告

## 测试结论

2026-10-20 正式部署与验收完成，代码提交 `f5fcb5b711656f785434dd2494d608ed01d5d757`。
模拟 Graph 不发送真实 Facebook 帖子；UI 夹具结果与生产读回分开记录。

## 测试范围

Page 共享配置/CAS/删除重建，用户草稿及记录隔离，固定来源有界查询，多素材跨页选择，
按语言五配对，独立文案/任务/八参数短链，重发确认，原操作幂等恢复，跨入口真实 Page
占用，旧新任务竞争，GPU/链接租约与取消，未知结果，当前主 API 与导航定点集成及回滚。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 新状态机（本地 SQLite + 模拟 Graph） | 41 | 41 | 0 | 0 |
| 来源、链接、FIFO SQL 适配器 | 29 | 29 | 0 | 0 |
| Cookie/权限/CSRF/身份映射 | 9 | 9 | 0 | 0 |
| 内部 broker/真实查询模板/集成边界 | 18 | 18 | 0 | 0 |
| 部署恢复与回滚竞态 | 7 | 7 | 0 | 0 |
| UI Playwright 断言，1440/820/390 | 71 | 71 | 0 | 0 |
| 旧自动发布针对性回归 | 94 | 94 | 0 | 0 |
| 旧素材及手动 CLI 回归 | 39 | 39 | 0 | 0 |
| 旧素材扩展回归 | 19 | 19 | 0 | 0 |
| 旧 GPU 客户端回归 | 17 | 17 | 0 | 0 |

部署恢复测试覆盖尚未迁移、复制一半、并发受理、保留历史链接和既有门禁恢复。
当前线上主 API 的真实文件变换已通过 Python 编译，当前 QuickNav 和新 JS 通过 Node 语法检查。
旧自动全量用例有一项 effects.service 用户断言与既有 root FIFO 运行约定不符，
不修改该无关服务；基线对照证据见 backend-verification.md。

## 缺陷情况

审查修复包括：短链预算预览、旧新 SQL 门禁、持久 attempt、未知请求重试误解锁、
过期异步租约回写、4 MiB 请求边界、opaque content_id、SQL 逗号/注释 JOIN 白名单绕过、
历史短链回滚保留、部分部署恢复。详见 bugs、sa-code-review、sa-test-review。

## 验证证据

代码测试驱动在 scripts/test_fb_video_publish_* 及 tests/test_fb_video_source_links.py。
本地 UI 截图和断言结果在 output/fb-video-ui（不提交生成物）；独立评审在
backend-verification.md、sa-code-review.md、source-link-contract.md。

## 遗留风险

生产 Page 授权、素材与第三方平台状态会变化，执行前仍须 fresh 校验；unknown 必须对账，
不能通过重新建批绕过。本次上线不使用真实 Facebook 发帖作为测试，Graph 上传及处理
状态转换使用模拟传输验证。计划日配额不等于实际发送日限额，积压可延后至其他日期。

## 发布建议

全部阻断项已修复并通过，按 deploy.md 完成 GitHub-first、数据盘备份和增量演练。
上线首次配置为空，没有导入测试 Page、草稿、任务或短链。

## 生产部署与真实验收（2026-10-20）

- 已推送分支 `codex/fb-video-publish-20261020`，CPU 从 GitHub 检出精确提交到
  `/mnt/data-disk/fb-video-publish/releases/f5fcb5b`。
- 服务器 Linux 再次执行 core 41、broker 18、deployment 7、source/link 29，全部通过。
- 05:55 UTC 执行上线；manifest：
  `/mnt/data-disk/fb-video-publish/backups/20261020T055525Z-f5fcb5b7/manifest.json`。
  在线 SQLite 备份、复制库增量迁移与 integrity_check 均通过；原 `fb_auto_*`
  非指标业务事实的逐表数量与 SHA256 在迁移前后相同。
- 60 个目标文件均逐一 SHA256 匹配。主 API PID 935255、FB sidecar PID 935230、
  新 worker PID 935322 处于 active/running，prepare timer active/waiting。
  原 8 个 FB timers 活跃状态全部保持；未部署任何 GPU 服务端源码。
- 06:01 UTC 复核新 Page、草稿、批次、任务、链接、attempt 均为 0；SQLite quick_check=ok，
  4 个跨模块保护 trigger 存在。主 API/FB sidecar/新 worker 上线后的 traceback 数均为 0。
- 新短链目录属于 fb-auto-post，0750，Nginx 有独立继承只读 ACL。
  公网未登录访问新 API 返回 401/cookie_auth_required；私有 broker 公网返回404；
  非法短链路径返回404。没有为验收分配生产短链编号。
- 已登录 Chrome 的真实 Cookie 路径完成 Page 空态、创建弹窗、2140 个真实候选 Page、
  剧语言和三宏示例、素材非空 50 条/页、翻页仍保留 2 条后追加第 3 条、
  未配置 Page 的引导、发布记录空态及刷新。未保存验证用配置/草稿、未提交正式批次。
  1440/820/390 的布局与完整五配对流程由独立 Playwright API 夹具覆盖。

### 隔离 GPU prepare-only

使用真实 10 秒 FB 源素材，独立数据库与稳定 job：
`fb-page-373583dba571beeeafec47b28bb7dc903babec3a895ede42`。
成片 HTTP 200，8,326,396 字节，SHA256：
`b57247496fa25f9c06a65ee5aa4636479b33c590feeaf49460938fc1263d68c0`。
CPU 对公开成片重新计算 SHA、ffprobe 核实 H.264 720×1280、AAC、9.984秒；
返回非空随机配方并冻结。隔离账本 attempt=0、link=0，未调用 Graph。

服务器证据：

```text
/mnt/data-disk/fb-video-publish/evidence/production-verification.json
/mnt/data-disk/fb-video-publish/evidence/prepare-only-20261020/evidence.json
/mnt/data-disk/fb-video-publish/evidence/prepare-only-20261020/public-readback.json
```
