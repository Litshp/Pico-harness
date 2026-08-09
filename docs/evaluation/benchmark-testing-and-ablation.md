# Pico 的 Benchmark、单元测试、集成测试与消融实验

## 1. 核心结论

Benchmark、单元测试和集成测试解决的不是同一个问题：

| 类型 | 主要问题 | 典型特征 | Pico 中的例子 |
| --- | --- | --- | --- |
| 单元测试 | 单个函数或类是否按预期工作 | 范围小、速度快、结果确定 | Memory 是否按规则召回，ContextManager 是否保留当前请求 |
| 集成测试 | 多个模块连接后能否正常工作 | 经过 Agent Loop、工具、Session 和文件系统 | `agent.ask()` 后是否执行工具、更新记忆并写入运行工件 |
| Benchmark / Eval | Agent 在一组任务上的表现有多好 | 固定任务、多次运行、聚合指标、对照实验 | memory on/off、context reduction on/off、真实模型任务通过率 |

三者并不是完全互斥的。Benchmark 可以复用集成测试的运行框架，但它们关注的问题不同：

```text
单元测试：这个零件是否正确？
集成测试：这些零件组装后能否工作？
Benchmark：整套 Agent 在一批任务上表现如何？
```

例如：

```python
def test_memory_retrieval():
    # 单元测试：验证一个局部行为。
    assert memory.retrieve("deploy") == expected_note
```

```python
def test_agent_uses_memory():
    # 集成测试：经过 Agent Loop、Prompt 和模型客户端。
    answer = agent.ask("之前的部署配置是什么？")
    assert "red" in answer
```

```text
相同的 30 个任务：
A 组开启 memory
B 组关闭 memory
每组重复 5 次，比较正确率、重复读文件、Token 和耗时
```

最后一种是消融实验：只改变一个功能开关，观察指标如何变化。

## 2. 什么情况下 Benchmark 能证明能力提升

“Benchmark 证明某项改造提高了成功率”必须满足一定条件：

1. 开启和关闭功能时使用相同任务。
2. 使用相同模型、模型参数、步数预算和 Token 预算。
3. 每个 Variant 从相同的初始工作区和 Session 状态开始。
4. 唯一变化是被研究的功能开关。
5. 每种配置重复运行多次，避免模型随机性影响结论。
6. 使用外部 Verifier 判断最终结果，不能只相信 Agent 的最终回答。

如果使用的是脚本模型，实验通常只能证明 Runtime 机制按设计生效；如果使用真实模型、真实 Coding Task 和外部 Verifier，才更接近证明真实 Agent 能力变化。

## 3. Pico 的功能开关

Pico 已经支持在实验中临时切换功能。`measure_feature_ablation_metrics()` 会构造以下 Variant：

```python
variants = {
    "full": {},
    "no_context_reduction": {"context_reduction": False},
    "no_memory": {"memory": False, "relevant_memory": False},
}
```

对应代码：[`pico/evaluation/metrics.py`](../../pico/evaluation/metrics.py#L158)。

`_temporary_feature_flags()` 会在实验结束后恢复原来的配置，避免一个 Variant 污染后续实验。

## 4. Working Memory 消融实验

### 4.1 实验设计

Pico 定义了 12 个记忆依赖任务，分为三类：

- `fact_lookup`：回忆之前从文件中读取的事实。
- `edit_dependency`：继续执行依赖先前文件约束的任务。
- `history_reference`：回答之前已经建立的历史结论。

任务定义位于 [`pico/evaluation/metrics.py`](../../pico/evaluation/metrics.py#L330)。

每个任务运行三种模式：

| Variant | 行为 |
| --- | --- |
| `memory_on` | 正常启用 working memory 和 relevant memory |
| `memory_off` | 同时关闭 `memory` 和 `relevant_memory` |
| `memory_irrelevant` | Memory 中存在内容，但内容与任务无关 |

关闭 Memory 的代码为：

```python
agent.feature_flags["memory"] = False
agent.feature_flags["relevant_memory"] = False
```

每个 Variant 使用相同的：

- 任务文件
- Bootstrap Prompt
- Follow-up Prompt
- 最大步数和审批策略
- 重复次数

实验循环位于 [`pico/evaluation/metrics.py`](../../pico/evaluation/metrics.py#L381)。

### 4.2 当前归档结果

当前归档使用 12 个任务，每个任务重复 5 次，所以每种 Variant 有 60 次运行：

| 指标 | memory on | memory off | irrelevant memory |
| --- | ---: | ---: | ---: |
| 正确率 | 100% | 100% | 100% |
| 重复读文件次数 | 0 | 60 | 60 |
| 平均工具步数 | 0 | 1 | 1 |
| 平均 Attempts | 1 | 2 | 2 |
| Memory 命中率 | 100% | 0% | 0% |

原始数据：[`memory-ablation-v2.json`](../../benchmarks/results/main-resume-repro-2026-06-07/memory-ablation-v2.json)。

### 4.3 这组实验能证明什么

这组实验能够证明：

> Working Memory 可以让 Agent 复用已经获得的信息，减少重复文件读取、工具步骤和模型调用轮次。

但它不能证明 Memory 提高了答案正确率，因为 memory on、memory off 和 irrelevant memory 的正确率都是 100%。Memory 关闭后，Agent 会重新读取文件，然后仍然得到正确答案。

还有一个重要边界：核心归档使用 `_MemoryExperimentModelClient` 脚本模型。该模型被明确编程为：

1. Prompt 中存在相关 Memory 时直接回答。
2. 不存在相关 Memory 时调用 `read_file`。
3. 读取后返回预设的正确事实。

对应代码：[`pico/evaluation/metrics.py`](../../pico/evaluation/metrics.py#L230)。

因此，这是一组可靠的机制测试，但不是充分的真实 LLM 能力证据。

## 5. Context Reduction 消融实验

### 5.1 实验矩阵

Pico 的上下文压力实验构造了以下矩阵：

```text
3 档 History 长度
x 2 档 Memory Note 数量
x 2 档 Request 长度
= 12 组配置
```

每组对比：

- `full`：开启 Context Reduction。
- `no_context_reduction`：关闭 Context Reduction。

实现位于 [`pico/evaluation/metrics.py`](../../pico/evaluation/metrics.py#L438)。

### 5.2 当前归档结果

| 指标 | 结果 |
| --- | ---: |
| 平均压缩前 Prompt 长度 | 6994.33 字符 |
| 平均压缩后 Prompt 长度 | 5575.67 字符 |
| 平均压缩率 | 16.36% |
| 最大压缩率 | 33.59% |
| 当前请求保留率 | 100% |

原始数据：[`context-ablation-v2.json`](../../benchmarks/results/main-resume-repro-2026-06-07/context-ablation-v2.json)。

### 5.3 这组实验能证明什么

它能够证明：

> Context Reduction 可以减少 Prompt 长度，并且当前实现没有裁掉最新用户请求。

但当前归档主要调用 `measure_feature_ablation_metrics()` 构建和测量 Prompt，没有真正要求模型完成 Coding Task。因此它不能单独证明：

> Prompt 变短后，模型的语义理解和 Coding Task 正确率没有下降。

## 6. Pico 已有的真实模型实验入口

Pico 的代码中还存在真实 Provider 版本的对照实验，但这些结果没有混入当前核心归档报告。

### 6.1 真实 Memory 实验

`run_real_memory_experiment()` 使用真实模型执行相同的 12 个任务，并对比：

- `memory_on`
- `memory_off`
- `memory_irrelevant`

记录指标包括：

- `correct_rate`
- `repeated_reads`
- `avg_tool_steps`
- `avg_attempts`

对应代码：[`pico/evaluation/metrics.py`](../../pico/evaluation/metrics.py#L852)。

### 6.2 真实 Context 实验

`run_real_context_experiment()` 对相同的 12 组配置分别运行：

- Context Reduction 开启
- Context Reduction 关闭

模型需要从长上下文中找出指定 Target Token，然后统计：

- 压缩前后 Prompt 长度
- `full_correct_rate`
- `raw_correct_rate`

对应代码：[`pico/evaluation/metrics.py`](../../pico/evaluation/metrics.py#L918)。

它比纯 Prompt 长度实验更接近结果质量评测，但任务仍然是 Target Token 检索，不是复杂的代码修改任务。

## 7. 当前可以和不可以宣称的结论

### 可以宣称

> Pico 通过开关消融验证了 Working Memory 可以减少重复读取和工具调用；Context Reduction 可以降低 Prompt 长度并保留当前用户请求。

这与当前核心报告给出的边界一致：[`pico-benchmark-core-report.md`](../../benchmarks/results/main-resume-repro-2026-06-07/pico-benchmark-core-report.md#L50)。

### 不能直接宣称

> Memory 和 Context Reduction 已经让真实 Coding Agent 的任务成功率显著提高。

原因包括：

- 核心 Memory 归档使用脚本模型。
- 核心 Context 归档主要测量 Prompt 长度。
- 任务规模较小。
- 没有报告真实模型多次运行的置信区间或统计显著性。
- Context 真实实验仍是 Token 检索，不是完整 Coding Task。

## 8. 更有说服力的评测方案

下一步可以准备 20～50 个真实 Coding Task，进行严格的配对实验。

### 8.1 控制变量

每组实验应保持：

```text
相同模型
相同任务
相同初始代码
相同 Prompt
相同 max_steps
相同 Token Budget
相同模型参数
相同审批策略
```

唯一变化是：

```text
memory on / off
context reduction on / off
```

每个 Variant 都应在全新的临时工作区和 Session 中运行，避免前一组实验留下的文件、History 或 Memory 污染结果。每项任务重复 3～5 次，用于降低模型随机性带来的误差。

### 8.2 使用外部 Verifier

不能仅根据 Agent 的 Final Answer 判断成功，而应检查最终工作区：

```bash
pytest
npm test
```

还可以检查：

- 目标文件内容是否正确。
- 不应该修改的文件是否保持不变。
- Git Diff 是否只包含预期改动。
- Agent 是否真正运行了验证命令。
- 运行工件中的状态与最终工作区是否一致。

### 8.3 推荐指标

| 指标 | 说明 |
| --- | --- |
| `task_pass_rate` | 任务最终是否通过外部验证 |
| `verifier_pass_rate` | 测试命令或规则验证是否通过 |
| `repeated_reads` | 是否重复读取已经获取的信息 |
| `avg_tool_steps` | 平均工具调用次数 |
| `avg_attempts` | 平均模型请求次数 |
| `input_tokens` | 输入 Token 消耗 |
| `cost_per_success` | 每个成功任务的平均成本 |
| `context_lost_failure_rate` | 由于关键上下文被裁剪导致的失败率 |
| `stale_memory_failure_rate` | 由于错误或过期记忆导致的失败率 |

对于同一个任务，应比较成对结果：

```text
两组都成功：功能没有改变该任务正确性
on 成功、off 失败：功能可能带来正向收益
on 失败、off 成功：功能可能造成信息丢失或误导
两组都失败：需要检查模型、工具或任务本身
```

除了平均值，还应保留每个任务的原始运行记录，避免汇总指标掩盖局部回归。

## 9. 面试回答建议

> Pico 已经具备 Memory 和 Context Reduction 的 Feature Flag 消融实验。现有核心归档主要验证机制收益：例如 Memory 开启后，12 个任务、每项重复 5 次的实验中，Follow-up 重复读文件从 60 次降到 0；Context Reduction 将平均 Prompt 长度压缩约 16.36%，同时保留全部当前请求。
>
> 但我会严格区分“机制生效”和“真实模型能力提高”。当前核心 Memory 实验使用确定性脚本模型，Context 核心实验主要测量 Prompt 长度，因此不能直接宣称真实 Coding Task 成功率显著提升。代码中已经有真实 Provider 对照入口，下一步还需要使用相同模型、相同 Coding Task、相同运行预算和外部 Verifier，进行多次配对实验，才能证明这些模块对真实 Agent 正确率和成本的影响。

