# JEV 动态上下文选择

Pico 将 JEV（TypeSafe System One）作为快速判断模型，而不是主代码生成模型。每轮主模型调用前，`JevContextSelector` 接收脱敏后的请求摘要、工具候选元数据、最近历史摘要和候选记忆，返回三类结构化判断：

- `tool_scope`：本轮只暴露 inspect、modify、execute、delegate 或 none 对应的工具 Schema；
- `history_scope`：选择 recent、relevant 或 minimal 历史窗口；
- `memory_scope`：保留相关笔记，或只使用工作记忆。

选择结果只改变 Prompt 动态区和原生 Tool Calling 的 Schema 列表。真正的工具注册表、参数校验、审批、路径围栏、命令熔断和 Seatbelt 仍由 Pico Runtime 强制执行，因此 JEV 不能借助低置信度判断扩大权限。

## 稳定区与动态区

```text
稳定 Prompt（可缓存）
  Agent 规则、协议说明、工具注册表签名

动态 Prompt（每轮重组）
  JEV 选择的工具 Schema、Workspace、Checkpoint、Memory、History

当前请求（完整保留）
```

没有 `TYPESAFE_API_KEY`、JEV 服务失败或三项判断的最低置信度低于 0.55 时，选择器回退到确定性全集；这条路径不改变 Pico 的安全边界，也不让外部服务故障变成任务失败。

## 环境

运行依赖为 `typesafe-sdk>=0.7.2`。本地 `.env` 使用 `TYPESAFE_API_KEY` 和 `TYPESAFE_MODEL=jev-1.12`，文件被 `.gitignore` 忽略；提交仓库只保留 `.env.example` 占位符。JEV state 只包含裁剪后的元数据，不包含 API key、完整工具输出或敏感环境变量。
