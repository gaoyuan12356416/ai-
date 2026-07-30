# 开发计划

## 开发范围

在既有 TT GPU worker 中增加可选 `local` 媒体后端，让 GPU 数据盘
成片通过独立 loopback HTTP 数据面和公网 HTTPS 入口直接提供给 TikTok。
现有 `cos` 后端保留；CPU 发布池、排期、账号 Token 信封和 TikTok
publish/reconcile 合同保持兼容。

本轮开发不开放真实 Direct Post。三项全局门禁继续关闭，现有带 Logo 和
推广片尾的媒体 profile 继续 `direct_post_eligible=false`。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求、SA、API、测试和部署设计 | PM/SA | `doc/021.tt-post-gpu-local-pull/` | 本文档已编制；评审结论为有条件通过 |
| 按后端分支校验运行配置 | 开发 | `features/tt_gpu/worker.py` | 已实现并回归 |
| 本地成片原子固化和不可变 URL | 开发 | `features/tt_gpu/worker.py` | 已实现并回归 |
| HMAC 路径、manifest 和文件身份校验 | 开发 | `features/tt_gpu/worker.py` | 已实现并回归 |
| URL Property verified origin 精确绑定 | 开发 | `features/tt_gpu/worker.py` | 已实现并回归 |
| prepared URL 实际 origin 逐次复验 | 开发 | `features/tt_gpu/worker.py` | 已实现并回归 |
| 只读媒体服务器 `GET`/`HEAD`/Range | 开发 | `features/tt_gpu/worker.py`、启动脚本 | loopback 已实现并回归；公网待验 |
| publish/reconcile 终态和清理生命周期 | 开发 | `features/tt_gpu/worker.py` | 已实现并回归 |
| 低磁盘水位与安全健康信息 | 开发 | `features/tt_gpu/worker.py` | 已实现并回归 |
| COS 旧行为与旧 manifest 兼容 | 开发 | GPU worker | 已实现并回归 |
| GPU 单元、合同与回归 | QA | `scripts/test_tt_gpu_worker.py`、TT/X 回归 | GPU 51/51；TT 全量 224/224 |
| root-only 环境、systemd 与 Nginx | 运维 | `deploy/` 和生产 GPU | 示例已完成；生产 Nginx 不启用 |
| DNS、80/443、安全组和可信证书 | 运维/域名负责人 | 外部基础设施 | 阻塞，待明确授权与完成 |
| TikTok URL Property 验证 | App 负责人 | TikTok Developer Portal | 阻塞，待人工完成 |
| GitHub-first CPU/GPU 发布 | 运维 | immutable release | 待执行 |
| 关闭态生产 canary 与回滚演练 | QA/运维 | GPU/CPU 生产环境 | 待执行；禁止 TikTok init |

### 实现顺序

1. 固定配置、manifest 和 URL 身份合同；选择后端前不读取另一后端密钥。
2. 实现 LocalMediaStore：工作文件复验后在同一数据盘原子落盘；
   既有对象复用必须校验大小和 SHA。
3. 实现 loopback media server：方法、路径、HMAC、普通文件检查、单段
   Range、HEAD 和断连处理。
4. 把 prepare 响应校验从 COS 专用逻辑抽象为当前后端的精确 URL 校验；
   旧 COS manifest 继续可读。
5. 在 publish/reconcile 账本中加入媒体生命周期：明确终态加安全宽限后
   才能清理；unknown/needs_review 不清理。
6. 增加磁盘水位 fail-close 和不含密钥/完整 URL 的健康、日志字段。
7. 完成单元、合同、TT/X 回归和独立代码评审。
8. 推送 GitHub 精确 commit，先以 `cos` 且三门禁为 0 部署代码；完成
   loopback canary 后再处理公网域名、TLS 和 `local` 关闭态切换。

## 编译 / 构建命令

```powershell
python -m compileall features scripts
python -m unittest scripts.test_tt_gpu_worker -v
python -m unittest discover -s scripts -p "test_tt_*.py" -v
```

还需执行既有 X 发布池和素材状态回归，确保 TT local 改造没有放宽 X
素材时长、触碰 X SQLite 或改变 X runner。

### 代码完成条件

- 配置、存储、媒体 Handler、生命周期和 COS 兼容用例全部通过。
- `GET`/`HEAD`/首中尾/suffix/非法 Range 的响应头和字节均被自动化固定。
- 路径穿越、签名、软链接、manifest 漂移和低磁盘均 fail-close。
- unknown/needs_review 在重启、清理扫描和重复 reconcile 后文件仍存在。
- 三项门禁与品牌门禁自动化断言 TikTok init 调用为 0。
- 三项原门禁全部为 1 但 verified origin 缺失/错配时，`ready=false`；
  后端切换后旧 origin 不能误开新后端门禁。
- 配置门禁 ready 但 prepared URL 实际 origin 不匹配时，publish 在
  TikTok init 前拒绝；`init_rejected` 与 `init_outcome_unknown`
  首版都不自动清理，5xx/限流/超时类响应必须落 unknown。
- `python -m compileall`、TT 全量、X 回归和 Git diff check 无异常。
- `sa-code-review.md` 与 `test-report.md` 在真实执行后填写；当前不得预填
  “通过”。

## 风险与依赖

- local 公网链路依赖可控域名指向、GPU 入站 80/443、安全组、Nginx、
  可信证书与续签；当前均不得从代码完成状态推断为已具备。
- TikTok 只允许已验证 URL Property 下的 HTTPS URL；该外部步骤必须由
  App 负责人完成并留截图/属性证据。
- HMAC URL 是媒体访问凭据，密钥须 root-only。首版若不支持多 key，
  pending 任务存在时不能直接轮换。
- 长视频和 unknown 可能长期占盘；需设置保守低水位、监控和人工核对，
  不允许自动删除未终态文件。
- GPU 外网带宽、连接数、Nginx 超时和 TikTok 拉取时长尚无生产负载数据；
  正式放量需另做容量 canary。
- 回退 COS 依赖现有 COS 配置仍有效。local 与 COS 不双写，因此回退只
  影响新 job，不能重写已冻结 job 的 URL。
- 真实 TikTok 发布涉及外部副作用；本轮自动化和关闭态生产验收都不得
  调用 init。

## 完成记录

- 2026-07-30：完成需求边界、SA 风险、API、测试与部署方案编制。
- 2026-07-30：完成代码实现、自动化和独立代码评审，GPU 专项 51/51、
  TT 相关全量回归 224/224 通过。
- GitHub 提交、COS 关闭态生产部署和健康检查待执行；公网 local 链路与
  TikTok 验收待外部条件。
