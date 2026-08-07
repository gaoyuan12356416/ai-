# 测试报告

## 测试结论

进行中。全部 TT Python 403/403 回归已通过，生产验收待完成。

## 测试范围

GPU source_direct/config/prepare/manifest/publish mock；既有 GPU 媒体模式、COS/local 存储、门禁、幂等和核对回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: | ---: |
| source_direct 定向 | 3 | 3 | 0 | 0 |
| GPU worker 全量 | 70 | 70 | 0 | 0 |
| 全部 TT Python 回归 | 403 | 403 | 0 | 0 |
| 生产验收 | 待执行 | - | - | - |

## 缺陷情况

未发现确认缺陷。首次定向运行遇到本机既有 `scripts/__pycache__` 权限冲突，改用独立 `PYTHONPYCACHEPREFIX` 后通过；该问题不属于产品代码。

## 验证证据

- source_direct 测试证明无 FFmpeg 命令，源/输出 SHA 和大小相等，manifest v6 可复用，mock 发布只 init 一次。
- GPU 70/70 通过，覆盖既有 branded/clean/outro、COS/local、发布账本、URL Property 和未知结果规则。
- TT Python 发现集 403/403 通过，覆盖账号设置、发布池、准备 runner、页面/API 合同、短码与链接。
- 生产最近 5 个源素材只读 ffprobe 均匹配窄合同。

## 遗留风险

- 尚未由用户执行真实 TikTok 原片帖子测试。
- 非标准源素材会 fail-closed，需要切回制作模式或另行评审放宽合同。

## 发布建议

完成 CPU 全量回归、GitHub push、双机备份和生产 health/自然调度验证后可切换；不由部署脚本主动创建真实帖子。
