# API 文档

## 接口列表

接口路径不变：模板列表、Page 池列表、运行列表/详情和内部计划/执行接口均沿用
现有合同。

## 请求/响应

字段结构不变。以下字段口径调整：

- `publishable_pages`：至少存在一条 `status<>1` 且 Token 非空的授权记录。
- `missing_token_pages`：不存在上述授权记录的 Page 数。
- `eligible_token_count`：`status<>1` 且 Token 非空的授权行数。

## 错误码

保留 `fb_page_missing_eligible_token`，兼容既有运行详情和失败处理；其含义更新为
“没有非被封且 Token 非空的授权”。

## 兼容性说明

- 无 API schema、配置或数据库迁移。
- 历史运行快照不重算。
- `status=1` 继续安全排除；其他状态按用户决策进入候选。
