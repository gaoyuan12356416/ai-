# 测试报告

## 本地结果

- GPU worker：68 项通过，覆盖无 Logo direct_outro、历史 branded preview、片尾转场、manifest/reuse 和发布 fail-closed。
- TT Post core：86 项通过，新增当前 profile 精确领取测试。
- Profile upgrade：4 项通过，覆盖 dry-run、双账本原子更新、错误身份不更新、reserved 排除和部署 unit 安全合同。

## 待部署验证

- 全量 TT Post 回归测试。
- CPU/GPU 精确 release 与 health。
- 现网 v1 -> v2 待发布素材迁移计数。
- 新成片抽帧无左上角 Logo。
- 发布历史计数不变，未主动触发真实发布。
