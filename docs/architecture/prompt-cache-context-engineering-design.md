# Pico 高效 Prompt Cache 上下文工程设计方案

> 研究依据：`/Users/zhangbohan/Documents/Codex/2026-09-26/xian-z/outputs/context-compression-analysis.md`，以及 Pico 当前的 `context_manager.py`、`prompt_prefix.py`、`runtime.py`、`checkpoint.py`、`providers/clients.py` 和现有评测文档。

## 1. 目标与结论

Pico 的目标不是把整段 Prompt 做成一个永远不变的字符串，而是把请求拆成：

1. 可以长期复用的稳定前缀；
2. 在一个 Session 内冻结、按版本更新的会话基线；
3. 每轮变化的运行状态；
4. 必须完整保留的当前请求。

Prompt Cache 只对前缀复用有效。因此核心设计是：**所有高复用内容放在前面，所有高变化内容放在后面；压缩只改动态历史，不重写稳定前缀。**

建议的目标 Prompt 结构如下：

```text
[Stable manual]
  Agent 规则、输出协议、工具 Schema、沙箱契约、schema_version

[Frozen session context]
  Session 启动时的项目约定、长期事实快照、初始 Workspace 基线

--- cache boundary ---

[Dynamic runtime state]
  Checkpoint、Working Memory、Relevant Memory、压缩后的 History

[Current user request]
  本轮用户请求，始终完整保留
```

稳定前缀的 hash 用于 Prompt Cache；Workspace 当前指纹、Checkpoint 版本和 History hash 用于恢复与审计，不应让它们每轮改变缓存前缀。

## 2. 从研究文档提炼出的工程原则

### 2.1 先做无模型减压，再做 LLM 摘要

DSH、Claude Code 和 pi 都体现了相同方向：工具输出截断、重复读取折叠、图片卸载、旧结果清理应先执行。只有规则处理仍然无法满足预算时，才调用摘要模型。

这样可以减少额外模型调用，也能把“选择错了内容”和“摘要写错了内容”分开诊断。

### 2.2 结构边界比摘要长度更重要

压缩范围不能从任意字符位置切断。至少要保护：

- system/manual 和工具定义；
- tool call 与 tool result 配对；
- 当前用户请求；
- 最近的用户消息和未完成工具链；
- 文件路径、测试命令、错误信息、提交 SHA 等可恢复锚点。

DSH 的 balanced region、Codex 的 replacement history、pi 的结构化摘要都说明：压缩单元应该是消息或完整 turn，而不是字符串片段。

### 2.3 压缩是可恢复事务

DSH 的 `start → summary → replacement → end` 和 Codex 的 compaction trace 值得直接借鉴。Pico 需要记录：源 History 版本、压缩范围、摘要 hash、稳定前缀 hash、压缩前后 token、失败原因和恢复状态。

摘要生成期间如果 History 或 Workspace 发生变化，必须重新验证版本；版本不一致时放弃提交，不能把旧摘要覆盖到新历史上。

### 2.4 长期记忆不能污染稳定前缀

Hermes 为了保持缓存，把 curated memory 冻结为 Session 启动快照；Pico 也应遵循这个取舍：

- 稳定的项目约定可以进入 frozen session context；
- 本轮召回的 episodic/durable memory 放在动态区；
- Session 中途新增的记忆先进入动态区，下一次 Session 或显式 refresh 才升级为冻结基线。

否则每次 memory 写入都会改变前缀，缓存命中率会持续下降。

## 3. 当前 Pico 的基础与主要缺口

当前本地改动已经完成了几个重要方向：

- `PromptPrefix` 已拆出 `stable_text` 和 `stable_hash`；
- `ContextManager` 已按 prefix、memory、relevant memory、history、current request 分层；
- 当前请求不会主动参与 section 预算裁剪；
- 历史中旧的重复 `read_file` 可以折叠，并可复用 file summary；
- OpenAI-compatible、Anthropic-compatible、DeepSeek 和 Ollama 已开始声明不同的 cache 能力；
- trace/report 已能记录 cache usage 和 prompt metadata；
- Checkpoint 的 `next_step` 已被放到文本末尾，降低 tail clipping 丢失恢复指令的风险。

仍需在后续阶段明确解决：

1. 预算目前主要按字符估算，尚未形成 provider/model 级 token meter；
2. Anthropic 的 cache breakpoint 目前通过字符串位置切分，字符偏移不等于 token 边界；
3. Workspace 文本、Checkpoint、Memory 和 History 的“动态区”还没有统一的 Prompt Envelope；
4. 压缩没有完整的事务状态机和 stale version 校验；
5. Provider 能力声明、payload 生成和 usage 解析还需要统一接口及更完整的 A/B benchmark。

## 4. 目标数据结构

### 4.1 Prompt Envelope

建议在 runtime 与 Provider 之间引入结构化对象，而不是只传一段字符串：

```python
PromptEnvelope(
    stable_blocks=[...],
    frozen_blocks=[...],
    dynamic_blocks=[...],
    current_request="...",
    manifest=PromptManifest(...),
)
```

`PromptManifest` 至少包含：

```text
schema_version
provider
model
tool_protocol
tool_signature
stable_prefix_hash
frozen_context_hash
workspace_fingerprint
history_version
dynamic_state_hash
stable_token_count
dynamic_token_count
cache_boundary_token
```

其中：

- `stable_prefix_hash`：规则、工具定义和固定协议的 hash；
- `frozen_context_hash`：Session 基线的 hash；
- `workspace_fingerprint`：恢复安全校验，不作为每轮 cache key；
- `history_version`：压缩事务并发校验；
- `dynamic_state_hash`：审计字段，不影响稳定前缀；
- `cache_boundary_token`：Provider 适配器计算的边界，不使用裸字符长度冒充 token 偏移。

### 4.2 Cache key

建议使用 provider/model 隔离的命名空间：

```text
pico:{provider}:{model}:{tool_protocol}:{stable_prefix_hash}:{frozen_context_hash}
```

不要把以下内容放进 key：

- 当前用户请求；
- 每轮时间戳；
- History 内容；
- Checkpoint 当前版本；
- Workspace 每轮状态 hash。

Workspace 或工具 Schema 真正改变时，应创建新的 `frozen_context_hash` 或 `tool_signature`，让缓存自然进入新 epoch。

## 5. 四阶段升级路线

### 阶段一：上下文压缩与预算裁剪

建议分支：`codex/runtime-context-reduction`

交付内容：

1. 引入统一 `TokenMeter`，至少支持字符估算、Provider 返回 token 和输出预留三种输入；
2. 预算计算采用：

   ```text
   usable_input = context_window - max_output_tokens - safety_headroom
   compact_when = measured_input >= provider_policy.threshold
   ```

3. 压缩顺序固定为：

   ```text
   tool result pruning
   → duplicate read collapse
   → image / large output offload
   → structure-safe LLM compaction
   → overflow retry
   ```

4. 结构化选择器保护 system/manual、工具配对、最近尾部和当前请求；
5. 摘要采用固定字段：`goal`、`constraints`、`progress`、`files_read`、`files_modified`、`tests`、`errors`、`decisions`、`next_step`、`anchors`；
6. 压缩前后记录 section token、释放 token、是否调用摘要模型和裁剪原因。

验收重点：

- 当前请求 100% 完整保留；
- 不拆开 tool call/result；
- 预算内完成率不低于现有基线；
- 长任务中重复读取和旧工具输出明显减少；
- 压缩前后能从 trace 重建选择范围。

### 阶段二：Prompt Cache

建议分支：`codex/runtime-prompt-cache`

交付内容：

1. 将 `PromptPrefix`、frozen session context 和动态状态组装为 `PromptEnvelope`；
2. 保证 stable blocks 的顺序、空白、工具 Schema 序列化和字段顺序确定；
3. Session 内冻结 curated memory 和项目基线，动态 memory 只进入 dynamic blocks；
4. 每轮只更新 dynamic blocks，不重写 stable blocks；
5. 记录 `stable_prefix_hash`、`frozen_context_hash`、cache boundary 和 cache usage；
6. 支持 warm-up、hit、miss、cache write、provider 不支持等明确状态。

验收重点：

- 只修改 Workspace 状态、Checkpoint、History 或当前请求时，stable prefix hash 100% 不变；
- 工具定义或规则变化时，hash 必须变化；
- cache key 不包含请求正文和动态 History；
- Provider 不支持的字段 0 次发送；
- 连续多轮请求能区分 warm-up、hit 和 miss，而不是只记录“supports=true”。

### 阶段三：Checkpoint / Prompt Resume

建议分支：`codex/runtime-checkpoint-resume`

交付内容：

1. Checkpoint 作为动态恢复块放在 cache boundary 之后；
2. 采用小型结构化 schema，包含 goal、blocker、completed、next_step、key_files、tests、workspace fingerprint；
3. Resume 时先校验 workspace fingerprint 和文件 freshness，再决定哪些 file summary 失效；
4. Checkpoint 更新不改 stable prefix；
5. 压缩事务绑定 `history_version` 和 `checkpoint_id`，防止旧摘要覆盖新状态；
6. 处理中断、上下文超预算、Workspace 漂移、Provider 失败和压缩取消。

验收重点：

- Resume Prompt 能看到完整 goal、blocker、next_step；
- Workspace 漂移能被识别，旧摘要不会被当作新事实；
- 恢复后不重复执行已经成功的破坏性工具调用；
- Checkpoint、History、Trace、Report 的 checkpoint id 能对应起来。

### 阶段四：Provider 缓存协议适配

建议分支：`codex/runtime-provider-adapters`

定义统一能力接口：

```text
PromptCacheCapabilities(
    strategy = none | explicit_key | breakpoint | automatic_prefix,
    supports_native_tools,
    supports_structured_blocks,
    supports_cache_telemetry,
    supports_retention,
)
```

Provider 行为建议：

| Provider | 缓存策略 | 请求设计 |
|---|---|---|
| OpenAI Responses | `explicit_key` | stable/frozen 内容保持前缀，只有明确支持的 endpoint 发送 `prompt_cache_key` 和 retention 字段 |
| Anthropic Messages | `breakpoint` | 用结构化 system content block 加 `cache_control`，边界由 token/block 计算，不按字符串切片 |
| DeepSeek Anthropic-compatible | `automatic_prefix` | 保持精确稳定前缀，不发送 OpenAI 专属 cache 字段，解析自动缓存 usage |
| Ollama | `none` | 不发送远端缓存字段，仍使用稳定 Prompt 布局，为本地 KV 复用留下空间 |
| 未知 Gateway | `none` | 默认关闭扩展字段，除非 endpoint 能力被显式配置并通过 payload 测试 |

验收重点：

- 每个 Provider 都有 payload 单元测试；
- usage 字段统一映射为 input、output、cached、cache write、hit ratio；
- 网络错误、HTTP 4xx、未知字段和 SSE/JSON 响应不会污染下一轮 cache metadata；
- Provider 不支持 native tools 或 cache 时，runtime 仍能回退到文本协议。

## 6. 评测与对照实验

使用现有固定任务集，至少运行以下配置：

| 配置 | Context reduction | Stable prefix/cache | Provider adapter |
|---|---:|---:|---:|
| baseline | off | off | legacy |
| reduction-only | on | off | legacy |
| cache-layout-only | off | on | mocked |
| combined | on | on | mocked/real |

每组任务至少记录：

- 输入 token、输出 token、cached input token、cache write token；
- stable prefix 命中率、动态区 token、cache boundary；
- 压缩次数、压缩前后 token、摘要延迟、overflow retry 次数；
- 工具重复读取次数、关键事实召回率、Checkpoint resume 成功率；
- 端到端任务成功率、预算内完成率、p50/p95 延迟和估算成本。

建议把验收分成硬门槛和优化目标。

硬门槛：

- stable prefix 在动态状态变化时 hash 不变；
- 当前请求完整保留；
- tool call/result 不被拆分；
- Provider 不支持的 cache 字段不发送；
- 过期 Workspace 状态不被无条件恢复。

优化目标：

- warm turn 的 cache hit ratio 达到 Provider 实际可达到的稳定水平；
- 长任务 Prompt token 显著下降；
- 任务成功率和事实召回率不低于 baseline 的 95% 区间；
- 摘要调用次数、overflow retry 和重复读取次数下降。

“压缩率更高”不能单独证明方案更好；必须和继续任务成功率、事实召回率、成本和延迟一起看。

## 7. 风险与决策边界

### 缓存失效过于频繁

原因通常是时间戳、随机 ID、动态 Workspace 摘要或不断变化的 memory 被放进前缀。解决办法是 canonical serialization、Session 冻结快照和 dynamic suffix。

### 缓存命中但事实过期

缓存命中只说明前缀相同，不代表 Workspace 状态没有变化。Workspace fingerprint 和 Checkpoint freshness 必须继续单独校验。

### 压缩后 Prompt 变短但任务变差

说明摘要损失了任务事实。应增加 schema/coverage 检查，保留路径、命令、测试结果和错误锚点，并回退到更大的 retain budget。

### Provider 能力误判

只根据 URL 字符串判断支持能力是不够的。应把 host allowlist、显式配置、payload contract test 和运行时 usage telemetry 结合起来。

### 自适应策略过早上线

动态阈值、重要性评分和自动摘要模型选择都会影响缓存边界和延迟。先做离线 replay，稳定后再按 provider/model/task type 放量。

## 8. 最终推荐顺序

```text
统一 TokenMeter 和结构边界
    ↓
无模型减压 + structure-safe compaction
    ↓
PromptEnvelope + stable/frozen/dynamic 分层
    ↓
Checkpoint 事务和 Resume freshness
    ↓
Provider-specific cache payload 与 telemetry
    ↓
固定 Benchmark + A/B + 受控放量
```

这条路线把 Prompt Cache 当成上下文架构的约束来设计，而不是在现有完整 Prompt 外面加一个 cache key。沙箱 Phase 1 保持冻结，四个运行时阶段各自独立测试、提交和发布。

## 9. 参考资料

- `context-compression-analysis.md`：Codex、DSH、Claude Code、Hermes Agent、pi 的上下文压缩、记忆和缓存分析。
- [`docs/architecture/state-memory-and-run-artifacts.md`](state-memory-and-run-artifacts.md)：Pico 的 Session、History、Memory、Checkpoint、Trace 和 Report 边界。
- [`docs/evaluation/benchmark-testing-and-ablation.md`](../evaluation/benchmark-testing-and-ablation.md)：固定 Benchmark、单元测试、集成测试和消融实验设计。
- [`pico/context_manager.py`](../../pico/context_manager.py)：当前 Prompt 分层、预算和历史折叠实现。
- [`pico/prompt_prefix.py`](../../pico/prompt_prefix.py)：当前 stable prefix 与 Workspace 状态拆分。
- [`pico/providers/clients.py`](../../pico/providers/clients.py)：当前 Provider 能力声明、缓存字段和 usage 解析。
