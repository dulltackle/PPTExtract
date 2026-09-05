## 实现完成后的自动推送

- 用户授权：在本仓库完成实现、通过必要验证并按提交规范提交后，自动将本次任务提交普通推送到 `origin`（`dulltackle/PPTExtract`）的当前同名分支，包括 `develop`，无需再次询问推送许可。用户在具体任务中另有限制时，以该限制为准。
- 推送前核对远程地址、当前分支与待推送提交；发现非本次任务提交或远程指向其他仓库时，先确认范围。
- 保留 Git 钩子和分支保护；遇到非快进拒绝、权限不足或审批拒绝时，保留本地提交并报告原因，不强制推送、不修改权限配置或绕过审批。

## Agent skills

### Issue tracker

Issues 存放在本仓库的 GitHub Issues 中，使用 gh CLI。详见 `docs/agents/issue-tracker.md`。

### Triage labels

使用默认的五个标准分类标签。详见 `docs/agents/triage-labels.md`。

### Domain docs

单一上下文布局（`CONTEXT.md` + `docs/adr/`）。详见 `docs/agents/domain.md`。
