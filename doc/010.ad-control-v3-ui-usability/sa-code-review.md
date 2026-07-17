# SA 代码评审

## 结论

代码评审通过；未发现需阻断发布的设计或实现偏差。生产数据与浏览器证据归入部署验收，不替代本评审。

## 评审范围

UI JS/CSS、Service cache/目录解析、FB adapter、同步脚本、测试和部署文档。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | sync script | 写入前必须证明源快照未漂移 | count + SHA256 双锁、事务 rollback | 已实现 |
| CR-002 | P0 | facebook adapter | 具体产品不能删除强制索引谓词 | 保留 dpdo/data_source/platform/product/date/optimizer/timeout | 已实现 |
| CR-003 | P1 | service cache | 并发缓存值可能被调用方修改 | 写入和返回均 deepcopy | 已实现 |
| CR-004 | P1 | UI save-preview | finally 重绘可能重新打开编辑器 | 成功时 editor=null，finally 仅 editor 存在时重绘 | 已实现 |
| CR-005 | P1 | UI dropdown | 首屏仍渲染全部原生 option | 改为菜单打开时才生成选项 | 已实现 |
| CR-006 | P1 | catalog evidence | 内部目录结构错误可能误报用户输入 400 | Service 与 Adapter 双层类型校验并统一 503 | 已实现 |

## 编译 / 验证结果

- `py_compile`、`node --check`、`git diff --check`：通过。
- 自动化共 190 项：UI/可用性 64、核心/路由/Repository/Live 98、部署与导航 28，全部通过。
- 真实浏览器完成优化师搜索、具体产品选择、Campaign 估算、能力卡删除和保存试算返回列表全链路。
- 生产收尾发现并修复 in-flight 标签残留，专项 36/36；再次试算后按钮无需刷新即可恢复。
