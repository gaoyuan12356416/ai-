# 部署文档

## 变更内容

素材池图片与软删除视频规则、共享图片媒体路径、历史错误重检。

## 配置项

无新增配置。

## 数据库变更

无 schema 变更；上线前执行在线 SQLite 备份和一致性校验。

## 部署步骤

1. 核对 GitHub 提交、active release、main composite 哈希和 ledger。
2. 暂停五个 X schedule/manual/auto timer，等待现有 oneshot 自然结束。
3. 在线备份 SQLite；Token 只记录 hash/mode/owner；备份代码/static/unit。
4. 从 GitHub 精确提交构建 immutable release并跑服务器测试。
5. 切换 Sidecar release，同步 exact Git 文件到 main runtime，窄重启 Sidecar/Main API。
6. 验证 health、匿名 401、SQLite、文件哈希和 ledger 不变量，恢复 timers。
7. 只观察自然 `no_due/no_pending`；禁止 run-now、手动任务或 canary Post。

## 验证步骤

待部署时填入实际命令、结果和计数。

## 回滚方案

切回部署前 immutable release，恢复 main runtime 文件与 timers，重启 Sidecar/Main API。默认保留当前 SQLite 和 Token。

## 注意事项

部署窗口如出现新生产提交，必须重新基于 live release 合并后再发布。
