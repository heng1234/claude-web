# CCW Desk 工作台参考与功能建议

核对日期：2026-11-18。比较依据是官方文档、官方产品说明、项目维护者的仓库与本地代码；没有逐一安装、登录或实测其他产品。CCGUI 在本文中指 `zhukunpenglinyutong/desktop-cc-gui`，Claude 以 Claude Code Desktop 为主要比较对象。

## 判断

下一阶段优先提高功能可发现性、配置易用性和结果可验证性。CCW Desk 已有相当多的底层能力，应优先将它们连成可见流程，再扩展原生 Codex 运行时和应用插件。

## 参考对象

| 对象 | 官方资料可以确认的方向 | 对 CCW Desk 的借鉴 |
| --- | --- | --- |
| Claude Code Desktop | 开发服务器预览、截图与错误检查、改动审查和 PR 状态；桌面文档也包含旁问、并行会话和插件 | 把启动、预览、反馈、验收与任务结果关联起来 |
| Codex | 项目内多线程与隔离 Worktree、Diff 审查、持续目标；公开集成层包括 SDK 和 app-server | 统一项目 / 任务状态，任务要有可检查的成功条件；接入 Codex 时复用其运行时接口 |
| Desktop CCGUI | 按会话使用不同 CLI 引擎，开发面板和命令面板；插件可以贡献界面组件 | 分清模型服务与 Agent 运行时；分清 AI 资源扩展与应用 UI 插件 |
| CloudCLI / Claude Code UI | 面向 Web / 手机的项目与会话界面，文件、终端、Git、浏览器操作，以及新增页面 / 后端服务的插件 | 保持 Web 使用优势，减少跨面板寻找入口的成本 |

CCGUI README 列出了插件 SDK；插件开发指南仍注明 v0.1 规范草案。本文借鉴其扩展点与权限模型，不据此保证全部接口稳定，也不推断这些插件能直接安装到 CCW Desk。

## 本地已有能力，避免重复立项

核对 `README.md`、`claude_web/server.py`、`claude_web/workbench.py`、`claude_web/workbench_api.py` 和工作台前端后，以下已有实现或入口：

- CLI 会话历史、会话分叉、文件回退与 Git checkpoint。
- 项目、Worktree、改动审查及文件级 Stage / Unstage。
- PTY 终端、文件浏览 / 查看、Project Map 和 Context Ledger。
- Agent Loop 的轮数 / 增量 Token 预算及测试重试；定时任务与通知。
- MCP 连接器及健康检查；本地 / ZIP / Git 扩展安装、组件及范围控制。
- 配置档、快捷命令、顺序工作流、任务审批 / 取消 / 恢复和配置分享。

“存在代码或入口”不代表所有组合和第三方环境均已通过实测，本记录不提升此前验收结论。

## 建议顺序

### 第一批：让现在的能力更容易用

| 建议 | 最小可交付范围 | 验收标准 | 工作量判断 |
| --- | --- | --- | --- |
| 工作台总览 | 最近项目、运行中 / 待审批 / 失败任务、常用配置档与工作流、简短开始指引 | 从首页能直接继续任务 / 处理审批；不必先进入扩展管理；状态来自真实数据 | 中 |
| 可视化工作流表单 | 添加 / 删除 / 上下移动步骤，步骤类型、输入参数、配置档、超时设置；JSON 留作高级编辑 | 表单与现有 JSON 可无损往返；运行前显示实际项目、输入和步骤；既有审批规则不变 | 中 |
| 会话生效配置面板 | 显示当前运行时、服务、模型、模式、权限、已启用扩展及来源范围；说明不可用操作 | 页面显示与真正发送字段一致；区分配置成功、协议测试通过和实际任务已验证 | 中 |
| 统一待处理列表 | 汇集工作流、Agent Loop、定时任务结果、待审批项；保留原模块的执行方式 | 项目过滤、打开原会话、审批、查看报告；不重复创建另一套任务状态 | 中至大 |

优先做前三项，再按任务来源逐个接入统一列表。不要一开始重写全部调度器。

### 第二批：完成开发与验收流程

1. 项目预览与验收：保存开发服务器启动配置、状态、端口、日志；由独立浏览器执行器产生截图、错误信息与断言结果，关联现有浏览器验收 Run。当前已有配方 / Run / 证据注册和等待逻辑，但该工作流步骤本身不会自动执行浏览器。静态 iframe 只能显示页面，不能当作已经验收。
2. 任务完成条件：在现有 Agent Loop 预算之上，显式配置测试命令、页面断言、文件产物或检查清单，并在结果页分别标出通过 / 失败 / 未验证。已有轮数和 Token 限额继续使用，费用若无供应商数据只能标为估算。
3. 轻量旁聊：在现有会话分叉能力上，增加不打断主任务的侧边问答，用户确认后将结论摘要带回主会话。文件改动仍留在原任务的权限与工作目录边界内。

### 第三批：原生多运行时与应用插件

**原生 Codex 运行时**

- 会话级区分 runtime 与 provider；不能将 OpenAI 文本兼容接口标成原生 Codex。
- 优先评估 Codex app-server：线程 / 回合生命周期、流式事件、工具审批、取消和恢复。
- 将 Claude 和 Codex 的能力差异暴露在界面，先做到两种运行时可靠，再考虑更多 CLI。
- 这是一项较大的接入工程，不承诺各运行时的权限、原生工具和会话文件可以直接互换。

**两层扩展体系**

- AI 资源扩展：继续支持现有 Skills、Agents、命令、MCP、Hooks、工作流。
- 应用插件：独立增加页面、状态卡片、命令面板动作和设置区块。第一版建议先提供声明式卡片与表单，不立即开放任意 JavaScript / Python 插件执行。
- 第一批应用插件示例：任务统计、项目启动卡、常用文档入口、模型能力看板。
- 安装时展示贡献点与权限，支持版本锁定、禁用及回滚。现有扩展 ZIP 不能直接当作受控的前端插件沙箱。

## 产品组织建议

- 左侧保留会话与工作台入口；工作台默认进入总览。
- 工作台内部按“总览 / 任务 / 工作流 / 能力与扩展 / 模型与环境 / 分享”组织；配置档与快捷命令仍可在能力区编辑，并在会话输入区就近应用。
- 主工作区使用页面，确认与短预览才使用弹窗；会话内使用轻量菜单或侧栏，不要求用户为了常用操作反复进入管理页。
- 不建议现在优先投入完整 IDE、复杂拖线画布、多人权限体系或十几个运行时。先把已有功能串起来，更容易形成可靠版本。

## 官方参考

1. Claude Code Desktop 文档：https://code.claude.com/docs/en/desktop
2. Anthropic，Preview / Review / Merge：https://claude.com/resources/articles/preview-review-and-merge-with-claude-code
3. OpenAI，Codex app 的项目 / Worktree / 审查设计：https://openai.com/index/introducing-the-codex-app/
4. OpenAI，持续目标与验收：https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex
5. OpenAI，Codex 运行时集成层：https://developers.openai.com/blog/codex-as-a-platform
6. OpenAI，包含旁聊等能力的跨设备工作流程：https://learn.chatgpt.com/blog/mastering-codex-remote-for-engineering
7. Desktop CCGUI 中文 README：https://github.com/zhukunpenglinyutong/desktop-cc-gui/blob/main/README.zh-CN.md
8. Desktop CCGUI 插件开发指南：https://github.com/zhukunpenglinyutong/desktop-cc-gui/blob/main/docs/plugin-development-guide.zh-CN.md
9. CloudCLI / Claude Code UI：https://github.com/siteboon/claudecodeui

以上参考用于比较设计方向；建议和优先级属于本项目的产品判断，不是其他产品的官方评价。
