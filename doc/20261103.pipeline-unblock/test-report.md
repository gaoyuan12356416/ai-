# 测试报告

## 自动验证

- `python -m unittest scripts.test_pipeline_tt_readback scripts.test_pipeline_tt_readback_error_refresh scripts.test_pipeline_tt_preflight_rejection_closure scripts.test_reconcile_fb_legacy_receipts scripts.test_deploy_pipeline_tt_readback_fix scripts.test_pipeline_monitor`：57 tests，7.026s，全部通过。
- 实际 Chromium 桌面、移动端、权限、并发编辑、旧深链、排空/忙碌/离线及保留 production ready 分阶段口径回归通过；截图保存在本聊天私有 work/backlog-20261103/draining-monitor-ui。
- CPU Python 3.9：FB+TT 恢复工具18项通过；Graph紧凑时区修正后 FB 11项再次通过。py_compile、node --check、git diff --check 通过。

## 业务验收（北京时间）

- 18:56:53 静态页面更新，公网3资源逐字节校验通过。没有重启 main API/coordinator/node。
- 19:01:06 TT 987 精确终结为已知前置拒绝失败；207次历史领取保留，0平台写，中央5693自然failed并释放原锁。
- 19:01 FB 29162/29303回填原视频2290597594846430/1045420691853155；中央1728/1750自然published、原锁清除；唯一publication各1条，仍按11月2日真实发布时间统计。
- 19:02中央回读：只有FB 29147原未知锁保留。19:01:54/19:02:04，原待制作3137/3139均在hk-gpu generation1开工。
- 19:03 GPU制作8条运行，19:08实测GPU运算59%、编码器33%、解码器7%。这是瞬时利用率，不是持续吞吐承诺。
- 19:05:15 TT原生服务无在途窗口单文件切换，PID60384→3741525；两台节点和coordinator/API PID未变。
- 19:07:35/39/44原有TT核对定时器查询3221/4094/4101，旧错误清空，status仍reconciling、原publish_id和attempt_count=1不变。
- 19:11最终配置v100：CPU制作6/发布12、GPU制作8/发布4，全局发布10。18:58前端管理员另行保存了CPU容量（v98/v99）；本次修复没有写容量或渠道开关，未把旧值回写覆盖。

## 限制与明确未做

FB 29147、TT1739/3232/4534/4535无确证结果继续隔离；TT3221/4094/4101平台仍processing，保留核对锁。共8条历史核对保护未解除。没有创建测试帖子、清空未知结果或重发上述历史任务。

X由独立修复恢复接单，本变更未操作X开关及其代码。GPU利用率恢复也包含X重新提供可领取任务的作用，不能全部归因于FB回填。

首次TT部署健康校验报告503：实际旧health检查要求已经被统一池接管停用的legacy runner.path。文件及进程已经切换，门禁全部清理；真实定时回查验证成功。补充验证记录单独保留，不篡改原审计，见deploy.md。

补充部署验证15项测试通过（0.289s）；19:12 canonical只读实际回执HTTP200与原库完全一致，旁路postverify成功，未再次重启。
