# SA测试评审

用真实临时SQLite验证身份审计发生在DELETE前，并测试写入失败时零DELETE、同attempt身份不可替换、重启保留审计和unknown锁。Graph使用模拟HTTP，断言目标ID和唯一DELETE调用，禁止把Page视频替代源Video。

复用177项原模块回归，独立新增Page隔离/动态重读/命名空间/退回User/明确失败与超时用例。前端DOM模拟只显示安全字段。

线上以只读probe核实真实Provider路径和GET /me的Page身份，不能据此声称Meta DELETE已经成功。无真实删除测试。
