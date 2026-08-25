# SA 代码评审

## 结论

通过，可进入 GitHub-first 发布。变更未扩大公网/API/SQLite 合约，没有真实发布测试路径。

## 评审范围

- selector 新旧路径的标签规则一致性。
- schedule 语言账号不存在与批容量耗尽的分支语义。
- Premium relay 既有回归与媒体修复回填边界。
- 香港 GPU systemd sandbox、loopback 监听、独立密钥/COS 配置和依赖冻结。
- 测试、部署、回滚与生产数据指纹。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | 中 | selector tests | 删除 tag 查询后旧“查询失败”测试不再触发 | 将故障注入移到仍存在的 drama 映射查询 | 已修复 |
| CR-002 | 中 | HK runtime | 系统 Python 3.6 不支持当前代码，且缺 COS SDK | 使用 Python 3.9 venv 与冻结依赖 | 已修复 |
| CR-003 | 低 | 部署 | 香港无 `/data` 独立挂载 | release 放 `/opt`、工作数据放 `/var/lib` 并由 systemd 限权 | 已修复 |

## 编译 / 验证结果

- `git diff --check`：通过（仅 Windows 行尾提示）。
- `python -m compileall -q features scripts`：通过。
- `python -m unittest discover -s scripts -p "test_x_post*.py"`：503 通过、1 跳过、0 失败。
