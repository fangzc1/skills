---
name: create-gitlab-mr
description: "在 GitLab 创建源分支到指定目标分支的 Merge Request。用于‘提 MR 到 test’‘当前分支合到 dev’等请求：先本地检查冲突，无冲突直接提 MR；有冲突从目标创建独立分支，只 cherry-pick 用户选定的提交并解决冲突后提 MR。不直接合并 MR、不推送目标分支、不部署；独立 cherry-pick 请求不适用。"
compatibility: "Git ≥ 2.38、Python ≥ 3.9（标准库）、GitLab HTTPS API；凭据 GITLAB_TOKEN，企业 CA 可设 GITLAB_CA_BUNDLE。"
---

# 简单高效地创建 GitLab MR

交付目标是 **MR 链接，不是合并或发布**。无冲突使用原源分支；有冲突使用基于目标的新分支，只迁移选定提交。这两条路径的内容范围不同，报告必须明确。

## 约定

- 默认源为当前分支已提交 HEAD；也可指定本地 `--source-branch`。源只在远端时先 fetch 到明确的本地分支并核对，不猜源。目标必须是用户明确给出的完整裸分支名。
- 先读仓库根规则，只有需要处理的路径才读目录规则；仓库禁止项、必需验证和用户确认限制优先。不因缺少规则文件阻断。
- 固定源、目标 SHA；未提交内容不带入，不 stash/reset/clean，不切换原工作区。
- 普通 push，不 force，不直接写目标分支、不批准/自动合并 MR、不部署、不改数据库。
- 用 `python -I <skill目录>/scripts/gitlab_mr.py` 调用以下阶段。确定性操作由脚本批量完成，模型只做范围选择、语义冲突和异常判断；不要逐条重复查询脚本已返回的事实。
- 结果以 JSON `status` 为准，退出码非零是执行错误。`blocked` 等状态即使进程返回 0 也必须停止；冲突路径为空不代表无冲突。凭据、网络及 hook 错误不能当冲突。

## 1. 预检：一次调用

```text
preflight --repo <repo> --target-branch <目标> [--source-branch <本地源>] [--check-only]
```

脚本检查仓库状态、fetch 最新目标、固定快照，执行 `merge-tree --write-tree`，返回 `snapshot`、`target_sha`、范围摘要及 `status`：

- `blocked`：按 problems 报告，停止；不自动修复仓库状态。
- `already_merged`：无需创建 MR。
- `clean`：直接进入 §3。**不建 worktree、不做 merge commit、不默认运行本地构建或测试**。本地无文本冲突不证明业务正确，未执行 CI 就不声称验证通过。
- `conflict`：仅从 `candidates.pending` 生成确认选项，`candidates.excluded` 只说明排除原因，不混入选项。用户已选定提交则交 §2 校验；未指定时即使仅一项也确认，不推荐全选、不从原始日志或标题补回已排除提交。两组各展示最多 10 条，`*_count` 是总数，`omitted_*` 是省略数；要选未展示项时按相同目标快照补查统一分类结果，不猜测。

`pending` 只表示“未找到等价补丁”，不是必需迁移或业务未合入的证明；`excluded.reason=patch_equivalent` 表示目标历史有等价补丁，不保证未被 revert。`source_ahead_count` 是祖先差集计数，不是待迁移数量；无冲突路径的原始范围日志同样不能用作候选来源。整分支冲突也不代表选定提交的 pick 冲突。无 pending 时报告“未发现新的迁移候选”，说明历史等价不排除 revert 等情况，不逼用户选提交。

用户说“只检查/不推送/不创建”时只执行此阶段，使用 `--check-only`，不访问写 API。不得为了等待 GitLab 算冲突而先创建临时 MR 或轮询状态。

## 2. 有冲突：只迁移选定提交

```text
resolve --repo <repo> --source-branch <原源分支> --target-branch <目标分支> \
  --target-sha <T> --source-sha <S> --commits <C1> [C2 ...]
```

源/目标分支名取自预检结果，与固定 SHA 一同传入，不从当前 HEAD 或 SHA 猜名称。新分支统一为 `resolve/<完整源分支>-to-<完整目标分支>-<YYYYMMDD-HHmmss>`（本地时间），如 `resolve/feat/TOKENHUB-Story-689-to-dev-20261124-183025`。名称不截断、不替换 `/`，创建前查重，不覆盖或自动改名已有分支；重名或目录前缀冲突时暂停说明。

脚本在写工作区前核对源分支、目标跟踪引用与固定 SHA 一致，再校验普通提交属于固定源快照，并复用预检分类排除目标祖先与历史等价补丁，再按拓扑顺序排列，查重分支名，从目标创建独立 worktree，交给 Git sequencer 逐个 cherry-pick。

- `ready`：迁移完成，保留返回的 worktree、分支和 `expected_mr_sha`，做最小必要检查。
- `conflict`：在返回的 worktree 处理冲突，取舍见 [冲突处理](references/冲突处理.md)；基线是目标，应用的是当前选中提交。只改冲突适配所需路径，不批量选边；意图明确直接处理，业务取舍不明一次询问。
- `equivalent_selected`：选中提交存在历史等价补丁，在创建 worktree 前已暂停。报告目标快照及排除依据；需要重新引入 revert 的改动时先明确意图，另行处理，不自动绕过检查或静默跳过。
- `blocked` 或执行错误：停止并报告。缺依赖不自动补提交；merge commit 不猜 mainline；空 pick 不静默 skip、不制造空提交。

冲突处理后显式 `git add -- <确认路径>`，再调用：

```text
resolve --repo <worktree> --continue
```

它会继续剩余已选提交，可能再次返回冲突；不要重复手工 pick 已完成的提交。多提交若存在依赖歧义，先确认，不自行扩大范围。

检查无未解决索引项、`git diff --check <T> HEAD`、最终差异只含选定内容及必要适配，构建产物不进入提交；对人工适配做最小定向验证，遵守仓库门禁，不能用空白检查代替行为验证。不默认全模块测试、不自动跑目标基线追查。失败则报告并暂停，保留真实退出码和完整诊断，未证实不能声称既有失败。运行时或环境依赖缺失时报告缺口，不擅自安装依赖、改锁文件或跑迁移；必要行为无法验证则等待补充方式或明确风险接受，不绕过硬门禁或已知新增失败。

## 3. 交付：一次调用

按项目规范准备标题和简短描述，写清原源分支及 SHA、目标分支及快照、实际 MR 源、选定提交、冲突取舍及实际验证/未验证项。用户要求 push 前确认时在这里停下。

```text
deliver --repo <repo或worktree> --source-branch <实际MR源> --expected-sha <M> \
  --target-branch <目标> --target-sha <预检T> --title <标题> --description-file <文件>
```

脚本复核本地源、重新 fetch 目标一次：目标前进但仍无文本冲突则继续；新冲突返回 `target_changed`，暂停，不循环重建。随后核对远端源，普通 push，确认远端 SHA，再创建或复用 MR。源变动、远端领先/分叉、迁移未完成或推送核验失败均停止。

GitLab 的 ff/半线性策略不满足时可创建 MR，但明确 `requires_source_sync`，不擅自同步源、不改迁移范围。`checking/unchecked` 仅报告处理中，不轮询、不称可合并。服务器可能因残余并发窗口与本地结论不同，最终合并由 GitLab 与人工审批决定。

只有 `created/reused` 且链接已核验才报告成功；`created_unverified/unknown/mismatch` 明确外部已完成动作和阻断，不盲目重发 POST，不修改已有 MR。仅凭据、权限、证书或连接异常时查 [认证与排错](references/认证与排错.md)。

最后只报告：MR 链接或阻断、源→目标、实际迁移范围、验证与未验证项、残留 worktree。无关完整 diff 和日志不进上下文。无论成败不自动删除分支或 worktree。
