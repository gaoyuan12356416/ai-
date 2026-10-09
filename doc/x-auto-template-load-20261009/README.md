# X 自动发布模板列表加载修复

模板接口正常返回已有模板，但列表渲染在读取嵌套 `config.drama_rule`、`config.material_rule` 时引用未定义的 `config`。异常被加载函数捕获后清空列表和统计，页面显示 `config is not defined` 和 0 条。

在每条模板的渲染作用域内读取 `ui.objectValue(item && item.config)`，保持已有顶层字段优先和嵌套配置回退逻辑。模板列表脚本缓存版本更新为 `20261009listfix1`；编辑页和账户语言契约保持原样。

## 验证

```powershell
python -m unittest scripts.test_x_auto_publish_ui scripts.test_x_auto_post_static_deploy -q
node --check static/x-auto-publish-templates.js
git diff --check
```

19 项现有 UI/静态部署检查通过。离线 Node VM 执行真实公共脚本和列表脚本，使用 DOM 适配器验证：原脚本重现 `config is not defined`；修复后嵌套配置、顶层规则、缺省配置和空列表正常渲染；刷新、查询、重置均通过，所有请求仅 GET。

部署前只读接口确认总计 3 个模板，2 个启用、1 个停用、0 个运行中。该数字仅为当时快照。浏览器控制连接的 request-header policy 加载失败，不能将脚本验证称为真实浏览器验收。

## 发布范围

从已推送的精确 GitHub 提交提取列表 JS/HTML。在已核实数据盘上备份主 API/Nginx 两套原文件，并构建当前 X 发布版本的静态修复副本，以便未来发布沿用修复。仅同步这两个静态资源并切换同代码版本目录，不重启服务、不修改发布开关、定时器、模板、任务或账本。

文件覆盖前校验原文件 SHA256 和 current 路径，避免覆盖并发部署。上线后验证三套资源一致、公网响应与缓存版本、真实列表 DTO 渲染及服务/定时器状态。回滚仅恢复这两个静态资源与原 current 指针，不恢复数据库或 Token。

具体提交、备份和回滚命令见本文件的上线验收记录。
