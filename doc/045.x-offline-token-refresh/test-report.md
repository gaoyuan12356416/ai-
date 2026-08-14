# 测试报告

## 测试结论

通过。修复满足发布条件，未进行任何真实 X 写入。

## 测试范围

X 账号授权、Token 刷新、素材/短剧/人工排期、X Auto、Premium relay、语言路由、UI、主 API 契约、队列幂等与未知结果保护。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| X Python 回归 | 667 | 665 | 0 | 0 |
| 既有条件跳过 | 2 | 2 | 0 | 0 |
| Python 编译 | 1 | 1 | 0 | 0 |
| JS/HTML 脚本语法 | 4 | 4 | 0 | 0 |
| Git 差异检查 | 1 | 1 | 0 | 0 |

## 缺陷情况

全量回归首次发现 1 个既有 race 测试未注入新增的最终 verify 前置条件；测试夹具已补齐，业务代码无需回退，复跑全量通过。

## 验证证据

- 过期且有 Refresh Token：`active + expired_refreshable + publish_eligible=true`。
- 过期且无 Refresh Token：`expired + publish_eligible=false`。
- Refresh Token 轮换一次并持久化新值；并发刷新只执行一次。
- 瞬时刷新失败时发布函数调用数为 0、账号仍可续期、日志为已知失败。
- X Auto Preview verify 调用数为 0；真实 Run 对每个账号 verify 一次后冻结 Task。
- Relay source 完整校验、target 按需刷新，第二次幂等读取不重复执行。

## 遗留风险

Refresh Token 可被用户撤销；明确撤销后仍需重新授权。部署恢复定时器后，错过的时间点按既有 grace/idempotency 规则自然判断，不人工补发。

## 发布建议

可发布。先做在线 SQLite/Token/代码/静态/systemd 备份，部署 GitHub 精确提交，只重启 Sidecar 与 X Auto，主 API 若仅静态同步则不重启；验证后按原状态恢复定时器。
