# Changelog

所有重要变更按版本记录。应用内的 What's New 和帮助面板使用 `static/changelog.json`，这里是给 GitHub / Git diff 浏览的 Markdown 版本。

当前稳定版本：`2.0.2`。

## Unreleased

- **FEATURE** Code 模式切换为 Claude Agent SDK 原生持久会话：常驻 Query 只接收本轮新消息，复用原生 Session ID，并直接转发 Claude 的流式事件、工具、Hooks、Skills、MCP、usage 和 compact 状态
- **FEATURE** 新增 Claude-only Node NDJSON bridge，支持多会话持久 runtime、原生 interrupt、上下文查询和会话释放，不包含 Codex SDK
- **FIX** 建立严格 runtime ownership：SDK / CLI owner 持久化到会话，失败时禁止隐式回退，旧 CLI 会话、CLI 导入和漏传 `workspace_mode` 都不会串线
- **FIX** Code 上下文圆环接通 SDK 真实 `getContextUsage()`；直接使用动态 `totalTokens / maxTokens`，本地模型返回 200k 或 1M 都会同步显示，圆环按剩余比例缩小
- **FEATURE** Code 手动与高水位压缩改为 Claude Code 原生 `/compact`，保持原 Session ID，并消费 `compact_boundary` 的压缩前后 Token；普通聊天仍保留本地摘要路径
- **FEATURE** 搬入 ccgui 风格的 `PreToolUse` 权限策略，并接通 Web 运行中审批：允许一次、本会话始终允许、拒绝和 AskUserQuestion 回答都会恢复同一个 SDK turn
- **FEATURE** claude-web 建立自己的 Agent SDK 安装机制：独立用户目录、精确 `package-lock`、临时校验、原子切换、启动失败回滚和设置页显式升级；ccgui 安装只作为迁移兼容
- **FIX** Git checkpoint v2 保存 tracked、staged 和 untracked 全部状态，以专用 Git ref 防 GC；禁止运行中回滚，并在恢复失败时事务式恢复点击时现场
- **REFACTOR** `claude_web/server.py` 成为唯一后端实现，根目录 `server.py` 只保留兼容启动转发
- **FEATURE** 已通过访问码或 Authenticator 认证的远程设备现在可使用与电脑端相同的工具权限、Agent Loop 测试命令和自动模式；Code 模式默认使用 `bypassPermissions`
- **SECURITY** 高权限开放后仅回环地址免登录；局域网、私网代理和公网客户端都必须先完成设备认证，管理功能仍限制在电脑本机
- **FEATURE** 明确 Code「轻上下文」只使用 Claude Agent SDK 原生会话与原生 compact；工具结果、diff 和思考的折叠仅是 UI 展示策略，不再自定义筛选后喂回模型
- **FEATURE** 设置页新增「轻上下文模式」「工具结果默认折叠」「思考过程默认展开」「上下文高水位提醒」四项偏好，均使用本地持久化
- **FIX** 浏览器刷新、切换网络或 SSE 断开后不再中止原生 Code turn；后端继续 drain 并持久化事件，页面重连会恢复运行状态与未处理的 Web 权限审批
- **FEATURE** Code 会话接通 SDK 原生 `setModel()`、`setPermissionMode()`、`forkSession()` 与 `rewindFiles()`；历史重试和编辑复用原生分叉，并持久化本地/原生消息偏移，避免回滚错轮次
- **FIX** Agent Loop 的 Token 预算改为“新增提示 + 模型输出”，不再把已有 200k/1M 上下文和 cache read 每轮重复计费
- **FIX** Node bridge 对同时存活的 Claude Query 设置 8 个硬上限，创建与占用原子化；满载时返回明确的 429，不再因并发竞态无限增长
- **FIX** Node bridge 到 Python 的 SDK 事件改用 4 字节长度帧，移除 `readline()` 的 64 KiB 隐式上限；大工具结果和图片不再触发 `Separator is not found, and chunk exceed the limit`
- **FIX** bridge reader 异常会立即标记 runtime 不健康、结束待定 turn 并在下次请求前换新 daemon；SDK 一接管本轮即回传 queued ACK，避免误判超时和 CLI 重放

## v2.0.2 - 2026-07-13

- **FIX** Code 工作区权限体验重做：默认使用全工具白名单，Bash、文件读写、搜索、Task、Notebook 等 Claude Code 常用工具不再频繁卡在「允许并重试」
- **FIX** 授权按钮不再被 `same-origin request required` 误拦；NAS、反向代理、移动浏览器壳和公网访问统一依赖访问码、Authenticator 或插件 Token 做安全边界
- **FIX** root / sudo 环境选择自动模式时改用 `acceptEdits + 工具白名单` 兼容运行，避免 Claude CLI 的 root 硬限制直接中断任务
- **FEATURE** Code 模式默认开启思考过程并发送中等推理强度；用户仍可在设置里关闭思考显示 / 发送或选择更高推理强度
- **FIX** 修改文件摘要只在本轮确实触发写入工具后显示，减少只读分析时误报文件改动
- **FIX** 当时的 Code 模式上下文显示改为 1M 级别估算，并暂停自动压缩；此估算方案已在 Unreleased 中被 SDK 真实 `getContextUsage()` 与原生 compact 取代

## v2.0.1 - 2026-07-12

- **FIX** Code 模式新增独立联网开关：关闭地球时禁用 WebSearch / WebFetch，但仍可通过 Bash 使用 curl 等本地命令
- **FIX** 优化 root / sudo 环境下的自动模式提示，避免远程 NAS 或虚拟环境部署时权限行为不清晰
- **FIX** 局域网访问识别扩展到 RFC1918 私网、loopback 和链路本地地址，同网段手机 / 电脑访问本机服务不再误要求访问码
- **FIX** 继续打磨 Code 工作区 UI：输入区上下文按钮、模型切换、思考折叠、工具结果、移动端布局和浏览器插件样式统一优化

## v2.0.0 - 2026-06-24

- **FEATURE** 主界面改为聊天优先：普通聊天隐藏项目、Git 分支、运行模式和本地执行上下文；切换到「代码工作区」后再显示开发能力
- **FEATURE** 侧栏重构为新会话、聊天、搜索、记录、CLI 导入和代码工作区的统一导航；项目绑定会话与普通聊天分区显示
- **FEATURE** 代码工作区输入框加入项目、分支、运行模式、Token 上下文、模型与推理强度；模型设置改为紧凑的主菜单 + 左右子菜单
- **FEATURE** 运行模式统一为默认、规划、代理和自动；常规安全命令可按当前模式直接执行，权限不足时仍支持本会话放行并重试
- **FEATURE** 代码回答使用更接近 Claude Code / Codex 的输出结构：隐藏普通聊天反馈和跟进建议，突出工具进度、命令、文件改动与 diff
- **FEATURE** 文件修改摘要支持打开查看差异；工具调用显示进度条、完成数量和折叠详情
- **FEATURE** 代码工作区显示上下文 Token 估算；长会话可手动输入 `/compact` 压缩历史
- **FEATURE** CLI 导入整合到侧栏导航，默认显示最近 10 条，继续滚动按需加载
- **FEATURE** 设置中心集中管理通知、手机访问、Authenticator、浏览器插件、费用统计和记忆
- **FEATURE** 浏览器插件 Side Panel 和设置页同步采用 2.0 视觉与移动端触控规范
- **SECURITY** Authenticator 支持设置界面和 `--setup-totp` 终端配置；本机与可信私网来源免验证，公网来源必须验证；转发头只在可信私网代理后生效
- **FIX** 手机搜索浮层移出会滑动关闭的侧栏 DOM，恢复完整宽度、结果标题和时间，不再只剩纵向图标窄栏
- **FIX** 普通聊天加载已有会话时不再错误显示项目和 Git 分支；项目上下文严格限制在代码工作区
- **FIX** 手机输入框、侧栏抽屉、模态框、安全区、代码块、表格、工具结果和 Edit Diff 的窄屏布局统一优化
- **DOCS** README、Changelog 和 Authenticator 指南统一更新到 2.0；README 截图全部替换为独立空数据目录生成的脱敏截图

## v1.6.1 - 2026-06-19

- **FEATURE** Agent Loop 升级为后端 job：前端只负责启动 / 订阅 / 停止，测试命令由后端在每轮后自动执行，并把结果反馈给下一轮 Claude；刷新或重开同一会话时会恢复仍在运行的 job 事件流
- **FEATURE** Agent Loop 增强可靠性：不填测试命令时自动检测 npm test / make test / pytest；单轮失败最多自动重试 2 次且不消耗正常轮次预算；连续 3 次相同测试失败会暂停并提示人工介入；每轮 checkpoint 在时间线中显示快捷回滚
- **FEATURE** 新增版本更新提醒：启动后检查 PyPI 最新版，发现新版时右下角提示并在帮助面板显示升级命令
- **FEATURE** 新增通知中心：Agent Loop 完成 / 阻塞 / 重复失败 / 出错和版本更新可推送到飞书、钉钉、企业微信、Slack、Discord、Telegram Bot 或自定义 Webhook；支持测试发送、事件开关、Secret 签名和最近发送记录
- **FEATURE** 新增手机访问模式：设置页自动识别本机局域网 IP，可生成 6 位访问码、自定义设备授权有效期、查看和撤销已授权设备；手机端进入问答模式，可提问 / 查看结果 / 从已保存项目切换目录，高风险管理操作自动禁用
- **FEATURE** 移动端体验升级：侧栏改为 off-canvas 抽屉并支持遮罩 / 关闭按钮 / 背景滚动锁定；900px 以下顶栏自动收纳，底部输入区适配 iPhone safe area，空状态快捷 prompt 改为横向滑动并显示更多提示
- **FEATURE** Agent Loop 自主工作模式：给定目标、轮数和 token 预算后，Claude 会自动连续执行、测试、修复，直到输出完成标记、阻塞、停止或预算用完，并可在结束时触发浏览器通知
- **FEATURE** Chrome 插件升级为当前页面上下文助手：点击插件图标直接打开 Side Panel，点「读取当前页」即可把整页可见正文带给 Claude；右键选中内容提问仍保留
- **FEATURE** 长会话新增里程碑标记：用户消息和回答都可点 ⭐，左侧「标记」Tab 只看关键轮次并一键滚动回原消息
- **FIX** 窄屏代码 / 工具渲染优化：代码块、表格、工具结果和 Artifact 代码视图支持横向滚动，Edit Diff 在手机上改为上下对比，避免长代码撑爆页面
- **FIX** 安全硬化：通知 Webhook 投递拒绝本地 / 私网 / 链路本地目标，移动访问 cookie 在反向代理 HTTPS 场景下正确设置 Secure 标记；登录页深色模式下补充清晰 focus ring

## v1.6.0 - 2026-06-16

- **FEATURE** 新增 Prompt 优化器：输入框工具栏可打开优化工作台，基于本地黄金样本库生成轻度优化 / 专家模式 / 探索模式三档 Prompt 改写，并可一键采用回填到输入框
- **FEATURE** Prompt 优化器支持从当前会话、手动粘贴、好评 / 收藏候选加入黄金样本；按任务类型自动分类，沉淀个人 Prompt 规则，展示规则置信度、来源样本数、相似成功样本和本地隐私提示
- **FEATURE** 新增 Chrome MV3「选中即问」插件：网页选中文本或代码后右键让 Claude 解释、审查、改写或生成测试，结果在 Chrome Side Panel 流式展示
- **FEATURE** 插件可一键转入完整 Claude Web 会话；Web 设置页支持生成插件 Token、显示插件目录、下载 ZIP，命令行新增 `claude-web --extension-path` / `claude-web-extension-path`
- **FEATURE** 新增 Agent Loop 自主工作模式：给定目标、轮数和 token 预算后，Claude 会自动连续执行、测试、修复，直到输出完成标记、阻塞、停止或预算用完，并可在结束时触发浏览器通知
- **FEATURE** Chrome 插件升级为当前页面上下文助手：点击插件图标直接打开 Side Panel，点「读取当前页」即可把整页可见正文带给 Claude；右键选中内容提问仍保留
- **FEATURE** 长会话新增里程碑标记：用户消息和回答都可点 ⭐，左侧「标记」Tab 只看关键轮次并一键滚动回原消息
- **FEATURE** 侧栏新增「CLI」Tab，与活跃 / 归档并列展示 Claude Code CLI 历史；导入列表标题优先使用真实用户消息，跳过 hook 注入内容
- **FEATURE** 回答反馈升级为好评 / 差评 / 收藏 / 原因 / 备注，并在统计面板展示反馈汇总；Prompt 优化器会复用好评和收藏作为黄金样本候选
- **FIX** Agent Loop 遇到 HTTP、SSE 或权限错误会停止循环，循环期间会拦截手动发送，避免失败后继续空转
- **FIX** 插件读取当前页时若页面读取权限未生效，会提示重新加载扩展，不再显示 `executeScript` TypeError
- **FIX** 修复 CLI 会话视图按钮初始化顺序导致列表加载中断的问题

## v1.5.5 - 2026-06-05

- **FEATURE** 活跃会话保温（Warm Process Pool）：每个会话保留一个持久 `claude` 进程，后续每轮跳过进程启动和 MCP 握手开销；无 MCP 省 1-2s，重度 MCP 用户（3-5 个 server）省 5-15s/轮；空闲 90s 自动回收，内存稳定在单进程量级（约 344MB）
- **FEATURE** 停止生成改用 `control_request interrupt` 信号而非 SIGTERM，进程不被销毁，下一条消息可立即复用保温进程；stdin 不可用时退回 SIGTERM；模型 / 权限策略 / cwd / 工具列表变更时透明重建进程
- **FIX** 同会话快速连发时不再弹「错误: claude exited with code 143」假错误——旧进程被安静替换而非误报崩溃（进程身份独立追踪）

## v1.5.4 - 2026-05-30

- **FEATURE** 全新「配置中心」面板（⚙ 按钮）：一个入口管 Hooks / Skills / MCP / Permissions 四个 Tab，支持 user / project / local 三 scope 切换，未保存改动有红点提示，关闭 / Esc / 切 scope 会拦截确认；保存前自动 `.bak` 备份，`ANTHROPIC_AUTH_TOKEN` 等敏感 env 在前端脱敏显示
- **FEATURE** Hooks 可视化编辑：PreToolUse / PostToolUse / Stop 等事件下的 matcher / type / command / enabled 在表格里直接增删改
- **FEATURE** Skills 管理：扫描 `~/.claude/skills/*/SKILL.md`，一键启停（文件级 SKILL.md ↔ SKILL.md.disabled）、可查看源码；英文 description 自动用 Haiku 翻成中文并本地缓存
- **FEATURE** Permissions 编辑：allow / deny 规则按行编辑直接落盘
- **FEATURE** Plan 审批卡片：Claude 调用 `ExitPlanMode` 时前端拦截并渲染成卡片，提供「✓ 批准并执行 / ✎ 编辑后批准 / ✕ 拒绝」三个动作；决定（含编辑后的 plan 文本）按 sessionId+toolId 持久化到 localStorage，刷新不丢；待审批时会自动停掉仍在跑的 Claude 进程让用户决定；批准 / 待审批用 pill 标签区分，适配暗黑模式
- **FIX** 配置中心 settings.json 并发写入加锁 + 原子替换，避免多个浏览器/标签同时保存时丢更新或写出半截 JSON
- **FIX** project / local 范围保存时正确路由到对应 .claude 目录，不再静默落到 `os.getcwd()`；前端缺少工作目录时 toast 中止
- **FIX** Skill 启用/禁用改为重命名 `SKILL.md ↔ SKILL.md.disabled`（Claude Code 启动时按文件名扫描），原先写入 `disabledSkills` JSON 字段对 CLI 实际无效
- **FIX** Skill 名称走严格白名单 + 路径解析校验，杜绝 `..` / 绝对路径越权访问 `~/.claude/skills` 之外
- **FIX** 配置中心 env 合并不再因前端未回传某个 key 就把已存在的环境变量删掉
- **FIX** Anthropic /v1/messages 调用只保留 `x-api-key` 头（去掉冗余 Bearer），默认翻译模型改用带日期的 `claude-haiku-4-5-20251001`，翻译失败改为 warning 日志而非静默吞掉

## v1.5.3 - 2026-05-28

- **FIX** Windows 上 claude-code 2.x 的原生 `bin/claude.exe` 启动器不再被错当成 JS 喂给 node，启动横幅显示真实版本而非 "unknown"，聊天 / compact / 智能命名等所有 CLI 调用路径一并修复

## v1.5.0 - 2026-05-23

- **FEATURE** 输入框新增完整 Slash 命令菜单：操作、重操作、内置模板和个人模板分组展示，支持 ↑↓ / Tab / Enter / Esc 键盘导航
- **FEATURE** 新增 /compact：用 Haiku 压缩旧会话历史，保留最近 2 轮原文并自动备份原 JSONL，显著降低后续上下文成本
- **FEATURE** 新增 /clear、/new、/fork：清空当前会话、新建会话、基于最后一条用户消息分叉到新会话
- **FEATURE** 新增 /init：扫描当前项目 README / package.json / pyproject.toml / 目录结构，生成 CLAUDE.md 草稿到输入框供用户审阅
- **FEATURE** 新增七个内置模板：/recap、/test、/explain、/review、/refactor、/commit、/docs；个人提示词可设置 slash_trigger 后显示在「我的模板」分组
- **FIX** Slash 命令支持只输入 / 即弹出菜单，同时避免 /usr/local、项目/ 这类路径误触发
- **FIX** /compact 与发送、停止、分叉、清空、删除互斥，避免压缩期间会话历史被并发改写
- **FIX** 切换会话、新建会话、发送消息时自动关闭 Slash / @ 引用浮层
- **FIX** 压缩备份会随会话删除一并清理，并自动保留最近 3 份或 7 天内备份

## v1.4.1 - 2026-05-21

- **FEATURE** 工具调用状态更清晰：运行中 / 成功 / 失败标识、耗时显示、参数与结果可折叠
- **FIX** 超时的子进程被强制回收，防止文件描述符泄漏导致服务卡死

## v1.4.0 - 2026-05-19

- **FEATURE** Artifact 面板新增 Code / Preview 切换，HTML / SVG 源码与渲染结果可对照查看；面板关闭后 artifact 仍保留，可从顶部按钮重新打开
- **FEATURE** 文档上传对齐 Claude.ai：单文件上限提升至 30MB，移除 5 万字硬截断；超 20 万字仅作 UI 提示
- **FEATURE** 新增 PPTX 支持（标题、正文、表格、备注、嵌套形状）
- **FEATURE** PDF 抽取优先 pdfplumber（表格 / 排版更好），失败回退 pypdf；每页加 [Page N] 前缀
- **FEATURE** DOCX 保留段落与表格原始顺序，包含页眉 / 页脚 / 首页 / 偶数页变体
- **FEATURE** 用户消息气泡显示已附文档徽章，点击打开预览弹窗；PDF 用浏览器原生 iframe 渲染并支持原文 / 文本切换 + 下载原件
- **FEATURE** 上传过程显示独立 spinner 与错误状态；删除加载中的 chip 会中止上传；发送按钮等待文档上传完成
- **FEATURE** 大 prompt（>60KB）改走 stdin 发送，避免 argv 溢出
- **FIX** 拖放浮层使用深度计数，避免拖过子元素时闪烁；仅对文件拖入触发
- **FIX** 文档 chip 错误信息进行 HTML 转义（XSS 防护）
- **FIX** 切换会话时清理 artifact，避免泄漏分离的 DOM 引用

## v1.3.0 - 2026-05-13

- **FIX** SQLite 启用 WAL 模式，免疫并发 "database is locked" 错误
- **FIX** 客户端断开 SSE 后自动清理后台 claude 子进程，不再产生幽灵进程
- **FIX** stderr 并发 drain，解决长会话偶发卡住不动的死锁问题
- **FIX** 会话历史 JSONL 原子写入，崩溃时不会损坏数据
- **FIX** 停止会话改用 SIGTERM + 3 秒后 SIGKILL 兜底
- **FIX** /api/files 优先使用 git ls-files，大型 monorepo 不再卡死
- **FIX** 前端图片上传去重：相同文件多次拖拽/粘贴不再重复上传
- **FEATURE** 启动时检测 claude CLI 是否安装，未安装时给出友好提示
- **FEATURE** graceful shutdown：Ctrl+C 关服务时清理所有运行中的子进程
- **FEATURE** 待发送的图片缩略图支持点击放大预览

## v1.2.0 - 2026-05-12

- **FEATURE** 上传不再限制文件类型白名单，任意文本文件都可上传
- **FIX** 智能编码检测：UTF-8 / GB18030 / UTF-16 自动识别

## v1.1.0 - 2026-05-09

- **FEATURE** MCP Server 管理面板：可视化查看 / 启用 / 禁用 / 新增（多 scope）
- **FEATURE** 支持上传 Excel（xlsx / xls）和粘贴板文件
- **FEATURE** 敏感字段自动脱敏显示（token / key / secret 仅显示前 4 位）

## v1.0.0 - 2026-05-01

- **FEATURE** PyPI 发包：pip install claude-web-ui 一键安装
- **FEATURE** Token 级流式输出、工具调用可视化、Edit 工具并排 diff
- **FEATURE** 多轮对话、停止任务、图片输入、文档上传、URL 抓取
- **FEATURE** 会话置顶 / 归档 / 标签 / 搜索 / 导出 / AI 智能命名
- **FEATURE** Git Checkpoint 回滚、历史消息分叉 / 编辑继续
- **FEATURE** TodoWrite 实时看板、统计面板、暗黑模式、移动端响应
