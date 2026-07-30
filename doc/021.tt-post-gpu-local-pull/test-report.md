# 测试报告

## 测试结论

2026-07-30 本地代码与 loopback 数据面核心自动化路径通过；仍有故障注入、
外网和实片用例待执行，生产 `local` 模式仍为 **阻塞**，不能据此开启真实
TikTok 发布。全部测试使用 Fake TikTok API，真实 init 调用数为 0，三项
生产门禁未改动。

## 测试范围

- GPU COS/local 配置分支、manifest v1/v2 兼容与确定性复用。
- GPU 本地文件原子持久化、SHA/大小/普通文件复验、磁盘入场预算。
- 签名 URL、GET/HEAD、完整/开放/suffix Range、If-Range、416、错误签名。
- 后端切换、URL Property origin 绑定、品牌 profile 门禁。
- init unknown、HTTP 5xx/限流类 fail-close、明确状态终态清理与身份冲突。
- TT core/service/UI/account/app 合同回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| GPU worker 自动化 | 51 | 51 | 0 | 0 |
| TT 相关全量回归（含 GPU） | 224 | 224 | 0 | 0 |
| 公网 HTTPS/DNS/TLS/Range canary | 1 组 | 0 | 0 | 1 |
| TikTok URL Property 与真实 Direct Post | 1 组 | 0 | 0 | 1 |

## 缺陷情况

独立代码评审发现的 P0/P1 均已修复并回归：后端回滚旧 URL 失效、prepared
origin 门禁错绑、清理身份不足、unknown 误清理、磁盘并发入场、掉电持久化、
fd 泄漏和 ready 缓存被长制作阻塞。最终复核无剩余 P0/P1。

## 验证证据

- TT 相关全量命令返回 `Ran 224 tests ... OK`。
- GPU 专项命令返回 `Ran 51 tests ... OK`。
- `python -m py_compile features/tt_gpu/worker.py` 通过。
- `git diff --check` 无格式错误；仅 Windows 工作树换行提示。
- 只读基础设施核查：`tt-media.ai.yingliangads.com` 仍为 NXDOMAIN；
  GPU 公网 80/443 不可达；80 端口被既有 Kronos 服务占用；无目标域名证书。

## 遗留风险

- 尚无公网域名、可信证书、云安全组放行和外网 Range 字节证据。
- TikTok URL Property 尚未验证。
- 当前带 Logo、Drama ID 和推广片尾的 profile 仍固定
  `direct_post_eligible=false`，不得真实 Direct Post。
- 当前官方文档说明初始化端点最长视频为 10 分钟；34.8 分钟成片不能作为
  真实发布 canary，仍须以所选账号实时 Creator Info 和平台合同为准。

## 发布建议

允许把代码按 `TT_POST_GPU_STORAGE_BACKEND=cos`、三项门禁全 0 的方式部署，
只做兼容性和健康检查。禁止切换 `local`、禁止启用 Nginx 公网配置、禁止
真实 TikTok init，直到部署文档中的外部阻塞全部解除。
