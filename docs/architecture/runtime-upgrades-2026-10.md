# Pico Runtime Upgrades

本次发布将三个运行时阶段合并到 `codex/priority-3-approval-snapshots`，并保持原有工具安全边界不变。

## 1. 上下文压缩与预算裁剪

- 将稳定规则、动态上下文和当前请求分区；当前请求始终完整保留。
- 压缩旧 History、重复 `read_file` 结果和过长工具输出，同时保留完整结果在运行工件中。
- 接入 TypeSafe Jev 作为快速判断模型，按请求动态选择工具 Schema、History 窗口和相关 Memory。
- Jev 只影响上下文装配，不拥有工具执行权限；服务失败或低置信度时回退到确定性集合。

## 2. Prompt Cache

- 稳定 Prefix 使用 canonical hash，Workspace、History、Checkpoint 变化不会使稳定缓存键失效。
- 增加模型级 cache epoch，避免不同模型错误复用缓存。
- 适配 OpenAI-compatible、Anthropic 和 DeepSeek 的缓存语义，记录 hit、miss、cached tokens 和创建 tokens。
- 对未知网关不发送未验证的缓存扩展字段。

## 3. Checkpoint / Prompt Resume

- Checkpoint 保存目标、约束、已完成动作、修改文件、测试记录、已知失败、下一步和 JEV 上下文选择结果。
- 使用 Workspace fingerprint、History version 和 History digest 识别恢复现场漂移。
- 恢复状态失效时重新生成 checkpoint，避免模型使用过期摘要继续执行。
- 运行过程继续生成 `task_state.json`、`trace.jsonl` 和 `report.json`，支持完整复盘。

## 环境与验证

- 新增运行依赖：`typesafe-sdk>=0.7.2`。
- JEV 使用 `TYPESAFE_API_KEY`、`TYPESAFE_MODEL` 和 `PICO_JEV_ENABLED` 配置；密钥只保存在本地 `.env`，不进入仓库。
- 191 项自动化测试全部通过。

本次提交明确不包含本地 `.env`、API key、`.agents/` skill 安装目录、简历、docx 和其他个人文件。
