# Changelog

所有重要变更按版本记录。应用内的 What's New 和帮助面板使用 `static/changelog.json`，这里是给 GitHub / Git diff 浏览的 Markdown 版本。

## v1.5.4 - 2026-05-30

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
