# JEV 动态上下文选择

Pico 将 JEV（TypeSafe System One）作为快速判断模型，而不是主代码生成模型。每轮主模型调用前，`JevContextSelector` 接收脱敏后的请求摘要、工具候选元数据、最近历史摘要和候选记忆，返回三类结构化判断：

- `tool_scope`：本轮只暴露 inspect、modify、execute、delegate 或 none 对应的工具 Schema；
- `history_scope`：选择 recent、relevant 或 minimal 历史窗口；
- `memory_scope`：保留相关笔记，或只使用工作记忆。

当前选择规模是固定且可审计的：`recent` 保留最近 6 条 History，`minimal` 保留最近 4 条，`relevant` 最多保留 10 条相关 History（没有匹配时回退到最近 6 条）。Jev 输入的 History 候选最多为最近 16 条。Memory 候选先由 Pico 的确定性检索取最多 3 条，再由 Jev 判断是否注入；`working` 只渲染任务摘要、最近文件和新鲜文件摘要，`none` 不渲染 Memory section。

确定性 Memory 检索不做分片或 embedding：每条过程笔记是一个最多 500 字符的原子记录，按“标签精确命中 → 关键词交集 → 时间新旧 → 插入序号”排序；持久化主题笔记使用同样的排序规则。当前关键词实现面向英文标识符和路径，对中文自然语言的召回能力有限，因此 Jev 只负责选择范围，不替代 Pico 的证据检索。

## Memory 与 History 的边界

- **History** 是完整事件流：用户消息、主模型回复和工具调用/结果都按顺序保存，用于恢复对话和复盘；`history_scope` 只决定本轮 Prompt 取其中哪些条目。
- **Working Memory** 是当前任务的短摘要、最近访问文件和带 freshness 校验的文件摘要，用于下一轮快速接续；工具执行成功后同步更新。
- **Episodic Notes** 是从工具结果和失败事件中提炼出的短过程笔记，例如文件摘要或“某工具被拒绝”；它保存在 session memory 中，按需召回，不是完整日志。
- **Durable Memory** 是用户明确要求沉淀的长期事实，写入 `.pico/memory/MEMORY.md` 和 `topics/*.md`，跨 session 使用。

Working Memory、Episodic Notes 和 History 都嵌在 `.pico/sessions/<session_id>.json` 中；Durable Memory 另存为 Markdown。当前写入是同步的：工具执行完成后更新内存，随后 `record()` 保存 session；最终答案触发 Durable Memory promotion 时同步写入主题文件。Trace、Checkpoint 和 Report 则分别记录过程事件、恢复快照和最终摘要。

选择结果只改变 Prompt 动态区和原生 Tool Calling 的 Schema 列表。真正的工具注册表、参数校验、审批、路径围栏、命令熔断和 Seatbelt 仍由 Pico Runtime 强制执行，因此 JEV 不能借助低置信度判断扩大权限。

## 稳定区与动态区

```text
稳定 Prompt（可缓存）
  Agent 规则、协议说明、工具注册表签名

动态 Prompt（每轮重组）
  JEV 选择的工具 Schema、Workspace、Checkpoint、Memory、History

当前请求（完整保留）
```

三个 Choice 的置信度取最小值；最低值必须达到 0.55 才采用 Jev 结果。没有 `TYPESAFE_API_KEY`、JEV 服务失败或三项判断的最低置信度低于 0.55 时，选择器回退到确定性全集；这条路径不改变 Pico 的安全边界，也不让外部服务故障变成任务失败。

## 环境

运行依赖为 `typesafe-sdk>=0.7.2`。本地 `.env` 使用 `TYPESAFE_API_KEY` 和 `TYPESAFE_MODEL=jev-1.12`，文件被 `.gitignore` 忽略；提交仓库只保留 `.env.example` 占位符。JEV state 只包含裁剪后的元数据，不包含 API key、完整工具输出或敏感环境变量。
