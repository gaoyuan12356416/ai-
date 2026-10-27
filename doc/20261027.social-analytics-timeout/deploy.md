# 部署与回滚

仓库 gaoyuan12356416/ai-，分支 codex/social-analytics-timeout-20261027。运行目录 43.166.187.96:/root/drama_material_service。仅更新 features/social_analytics/{routes,report,scoped_metrics}.py。

从 GitHub fetch 的精确提交工作树运行：

```
python3 scripts/deploy_social_query_timeout.py stage
python3 scripts/deploy_social_query_timeout.py activate --backup <stage 返回的 backup>
```

stage 检查数据盘 UUID、空闲空间、现网 SHA、干净且远端可追溯的 release，保存被改文件。activate 原子替换、导入校验、仅重启 drama-material-api.service，检查发布进程 PID 与独立日报 release。失败自动回滚代码。

精确回滚：在相同 release 运行 `python3 scripts/deploy_social_query_timeout.py rollback --backup <backup>`。拒绝后续文件漂移，不恢复 SQLite、Token、短链或历史报表。

实际提交、备份、执行和验收结果另见 deployment-evidence.md。
