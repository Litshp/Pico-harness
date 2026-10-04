# Phase 1 Sandbox Upgrade

本次更新为 Pico 增加第一阶段的纵深防御能力，按三个独立优先级实现并验证：

## 1. macOS Seatbelt 沙箱

- `run_shell` 默认在 macOS 上通过 `/usr/bin/sandbox-exec` 启动。
- 使用 deny-by-default profile，仅允许工作区读写和启动工具所需的系统读取路径。
- 拒绝网络访问，重定向 `TMPDIR` 到 `<workspace>/.pico/sandbox-tmp`。
- Shell 超时会终止整个进程组，避免子进程继续运行。
- 支持 `--sandbox auto|seatbelt|disabled`；强制 Seatbelt 不可用时失败关闭。

## 2. Shell 危险命令熔断

工具执行前会拒绝明显破坏性命令，包括：

- `sudo`、`mkfs`、写入 `/dev` 的 `dd`
- `rm -rf /`
- `shutdown`、`reboot`、`halt`、`poweroff`
- 典型 fork bomb

该策略是快速失败前置检查，不能替代操作系统级沙箱和审批机制。

## 3. 审批快照与变更预览

- 审批请求绑定工具参数哈希和 Workspace 指纹。
- 写文件、补丁工具展示影响路径和 unified diff 预览。
- 参数或工作区在审批后发生变化时，执行会被拒绝并记录 `approval_stale` 安全事件。
- Web 审批界面展示风险等级、影响路径、参数和差异预览，并提交快照校验字段。
- 兼容原有 `(name, args)` 审批回调，同时支持结构化审批请求。

## 验证

- 完整测试套件：`186 passed`
- 沙箱、命令策略、审批和安全回归专项：`49 passed`
- 三个阶段分别使用独立 Git 提交，并在干净 worktree 中完成最终验收。

阶段提交：

| 优先级 | 分支 | 提交 |
| --- | --- | --- |
| Seatbelt 沙箱 | `codex/priority-1-seatbelt-sandbox` | `7b2164a` |
| 命令熔断 | `codex/priority-2-command-policy` | `d971301` |
| 审批快照 | `codex/priority-3-approval-snapshots` | `5382970` |
