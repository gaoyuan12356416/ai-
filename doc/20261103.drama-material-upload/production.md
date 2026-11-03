# 生产验收 2026-11-03

代码已从 GitHub 精确版本部署到 CPU 43.166.187.96:/root/drama_material_service。

- 主功能提交：629dd43e45fbc0216336da597d043acd99b44f98。
- 导航页面补齐提交：d0a142bf530155b2445bde1b258f77b4552d9d8a。
- 主功能备份：/mnt/data-disk/drama-material-source/deployments/20261103T082920Z-629dd43e45fb/manifest.json。
- 导航静态备份：/mnt/data-disk/drama-material-source/deployments/20261103T083249Z-d0a142bf5301/manifest.json。

本地与CPU的39项Python测试通过；10项Node前端测试通过。组合代码编译、静态脚本语法、Chrome完整页面模拟操作通过，无浏览器错误。单产物提交、等待状态、后台完成后自动显示素材ID、多产物选择已验证。未向正式素材API发送测试视频。

生产核对：主API及独立drama-material-source.service均active，上传worker已enable，NRestarts=0；公开首页与/drama-synthesis.html均HTTP200且内容与部署文件完全一致；runtime脚本HTTP200并与线上文件一致；匿名新接口401。真实已完成任务DTO、公开URL、原剧国家语言只读查询、API配置均通过。上传SQLite完整性ok，验收时上传记录0条。合成及封面任务状态计数保持原样。所有部署目标和备份SHA核对通过。导航页补齐未重启服务。

## 精确回滚（仅代码）

在CPU执行，先静态导航副本，后主功能；始终保留上传数据库和素材回执：

```bash
cd /mnt/data-disk/drama-material-source/releases/d0a142bf530155b2445bde1b258f77b4552d9d8a
python3 scripts/deploy_drama_material_source.py --rollback /mnt/data-disk/drama-material-source/deployments/20261103T083249Z-d0a142bf5301/manifest.json
python3 scripts/deploy_drama_material_source.py --rollback /mnt/data-disk/drama-material-source/deployments/20261103T082920Z-629dd43e45fb/manifest.json
```

回滚会校验当前文件及备份，主功能回滚等待合成/封面空闲，停用新增上传worker并重启主API。数据库不回退，未明提交不可重发。后续再上线必须沿用原账本。
