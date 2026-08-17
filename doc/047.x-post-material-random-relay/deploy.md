# 部署文档

## 变更内容

普通 material schedule 增加稳定随机目标配对与非会员长视频 Premium relay Post -> Repost。

## 配置项

无新增环境变量。workflow version 固定为代码常量 `material-random-relay-v1`。

## 数据库变更

无新列/表。`ensure_storage` 幂等重建 relay insert/update triggers，使 material relay 仅允许 `schedule_run_id IS NOT NULL`。历史行不重写。

## 部署步骤

1. GitHub-first：提交/推送审核 commit，确认基线包含生产复合版本；本任务未执行。
2. 暂停相关 X schedule/manual/auto timers，记录原状态；禁止 run-now。
3. 对在线 SQLite 使用 backup API，备份 release、unit/env、非 secret token hash/mode/owner；不得读取 Token 正文。
4. 生成不可变 release，先在备份副本运行两次 migration 与历史投影 diff。
5. 切换 Sidecar release，并同步 main API 的同一 `service.py`；仅重启受影响服务。
6. 恢复 timer 原状态，观察自然 `no_due/no_pending` 或自然计划，不创建真实 Post canary。

## 验证步骤

- exact release 运行专项与 focused server suite。
- `quick_check=ok`、foreign key=0，历史 queue/log/repost projection 不变。
- Sidecar/main API `service.py` hash 一致。
- 自然 timer 后核对 queue/log/pool/repost ledger；无 real X Post/Repost 作为部署证明。

## 回滚方案

1. 暂停相关 timers，切回上一不可变 release，恢复 main API 对应文件并窄重启。
2. 恢复 timers 原状态。
3. 默认保留当前 SQLite 与 Token 状态；新增 trigger 向后兼容，禁止用旧 SQLite/Token 覆盖生产新事实。
4. 若存在已冻结 material relay queue，回滚前先审计；不得删除 queue/ledger 或盲重发 unknown source/repost。

## 注意事项

- 本文档仅为部署方案，本次交付未 commit/push/deploy。
- 严禁真实 X Post/Repost、run-now、manual publish 验证。
