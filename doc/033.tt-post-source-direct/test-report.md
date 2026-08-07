# 测试报告

## 测试结论

通过。全部 TT Python 403/403 回归、双机候选 release 验证、生产 health 和三轮自然调度验收均已完成；未触发真实 TikTok 发布。

## 测试范围

GPU source_direct/config/prepare/manifest/publish mock；既有 GPU 媒体模式、COS/local 存储、门禁、幂等和核对回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: | ---: |
| source_direct 定向 | 3 | 3 | 0 | 0 |
| GPU worker 全量 | 70 | 70 | 0 | 0 |
| 全部 TT Python 回归 | 403 | 403 | 0 | 0 |
| CPU 候选 release TT 回归 | 403 | 403 | 0 | 0 |
| GPU 候选 release source_direct 定向 | 2 | 2 | 0 | 0 |
| 生产自然调度观察 | 3 | 3 | 0 | 0 |

## 缺陷情况

未发现确认缺陷。首次定向运行遇到本机既有 `scripts/__pycache__` 权限冲突，改用独立 `PYTHONPYCACHEPREFIX` 后通过；该问题不属于产品代码。

## 验证证据

- source_direct 测试证明无 FFmpeg 命令，源/输出 SHA 和大小相等，manifest v6 可复用，mock 发布只 init 一次。
- GPU 70/70 通过，覆盖既有 branded/clean/outro、COS/local、发布账本、URL Property 和未知结果规则。
- TT Python 发现集 403/403 通过，覆盖账号设置、发布池、准备 runner、页面/API 合同、短码与链接。
- 生产最近 5 个源素材只读 ffprobe 均匹配窄合同。
- CPU、GPU health 均报告新 profile/mode 健康，CPU 到 GPU 的 `127.0.0.1:18830` tunnel health 正常。
- 17:37、17:38、17:39 三轮自然调度的 prepare 均 `claimed_count=0`，runner 均 `publish_request_count=0`。
- 部署前后数据库关键基线一致：queue max `91`、run max `95`、`publish_id=89`、active `0`、unknown `0`；`PRAGMA integrity_check=ok`。

## 遗留风险

- 尚未由用户执行真实 TikTok 原片帖子测试。
- 非标准源素材会 fail-closed，需要切回制作模式或另行评审放宽合同。

## 发布建议

生产已切换为 `source_direct`，可以由用户按原业务入口创建一条测试；部署与验收过程没有主动创建真实帖子。
