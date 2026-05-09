# Claude Code Web

[![PyPI version](https://img.shields.io/pypi/v/claude-web-ui.svg)](https://pypi.org/project/claude-web-ui/)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)

一个给 [Claude Code](https://docs.claude.com/claude-code) CLI 加可视化界面的 Web 应用。后端用 FastAPI 包装 `claude -p --output-format stream-json`，前端通过 SSE 流式渲染对话、工具调用、思考过程。

```bash
pip install claude-web-ui && claude-web
```

> 🔒 **隐私说明**：本工具只是 `claude` CLI 的本地 GUI 包装器，不上传任何数据到第三方服务。所有对话、图片、会话历史都存在本机 `history/` `uploads/` `claude-web.db` 中。认证沿用你本地 `claude` 的登录态（`~/.claude/`），本工具不接触任何 API Key。

## 📸 截图

### 主对话界面
![主界面](screenshots/main.png)

### Token 级流式输出 & 工具调用可视化
![流式 + 工具](screenshots/stream.png)

### Edit 工具并排 Diff
![Edit Diff](screenshots/diff.png)

### 使用统计面板
![统计](screenshots/stats.png)

### 暗黑模式
![暗黑模式](screenshots/dark.png)

## ✨ 特性

### 💬 对话
- **Token 级流式输出**（打字机效果）
- 多轮对话（基于 `claude --resume`）
- 停止正在运行的任务
- **跟进建议**：回答后自动生成 3 个「你可能想继续问」的追问按钮
- **会话分叉**：基于任意历史消息编辑 / 重新生成，原会话保留
- **思考动画**：等待响应时用跳动圆点 + 扫光文字提示

### 📝 输入
- 文本 + 图片（**文件选择 / 粘贴 / 拖拽**）
- **文档上传**：PDF / DOCX / CSV / TSV / TXT / MD / JSON / LOG 自动提取文本作为上下文
- **URL 自动检测**：输入框里粘贴链接，发送时自动抓取网页正文
- **联网搜索开关**：一键激活 WebSearch / WebFetch
- `@` 引用工作目录下的文件（↑↓ 选择）
- Token 估算 + 草稿自动保存
- 提示词模板库

### 🎨 渲染
- Markdown + 代码高亮（highlight.js）
- 工具调用图标化（Bash / Read / Write / Edit 等）
- **Edit 工具并排 diff**
- **Mermaid** 图表 + **LaTeX** 公式
- **代码块一键运行**：Python / JavaScript / Bash 现场执行，输出嵌在对话里
- 图片 Lightbox（点击放大）
- 代码块 / 全文一键复制
- **滚动控制**：流式输出中手动往上滚不被打断，右下角浮动「跳到最新 ↓」按钮带新内容计数

### 🗂 会话管理
- 📌 置顶 / 📥 归档 / 🏷 标签
- 🪄 AI 智能命名（让 Claude 给会话起标题）
- 双击标题重命名
- 搜索（标题 + 内容）
- 导出为 Markdown

### 🛡 安全 & 回滚
- **权限策略**：自由 / 允许编辑 / 计划 / 只读 / 自定义工具列表
- **工具权限重试**：当 Claude 因权限不足无法使用工具时，UI 弹出提示卡片，支持一键放行工具并重试本轮（无需切到 CLI）
- **Git Checkpoint**：每轮对话前自动 `git stash create` 快照，一键回滚文件
- **编辑 / 重新生成**：基于任意历史消息分叉新会话
- **SSRF 防护**：URL 抓取拒绝私网 / 本地主机

### 📋 TodoWrite 实时看板
- Claude 调用 TodoWrite 时右上角弹出任务面板
- 进度条 + 逐项状态（⬜ 待办 / ⏳ 进行中 / ✅ 完成）
- 多次 TodoWrite 调用自动队列回放，进度动画
- 面板可折叠 / 关闭

### 📊 其它
- 模型切换（Opus / Sonnet / Haiku）
- 使用统计（总成本 / 每日成本柱图 / 工具使用排行）
- Git 状态栏（branch / dirty 文件数）
- 系统提示词自定义
- 暗黑模式
- 快捷键：`⌘K` 搜索 · `⌘N` 新会话 · `Esc` 关闭弹窗
- 浏览器通知 + 完成提示音
- 移动端响应式（侧栏可收起）
- IME 输入法兼容（中文拼音回车不误发）

---

## 🚀 快速开始

### 前置条件

1. **已安装 [Claude Code CLI](https://docs.claude.com/claude-code/quickstart)**：
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude  # 首次登录
   ```
2. **Python 3.9+**

### 安装

#### 方式一：pip 安装（推荐）

```bash
pip install claude-web-ui
```

安装完成后一条命令启动：

```bash
claude-web
# 浏览器打开 http://127.0.0.1:8765
```

更新到最新版：

```bash
pip install --upgrade claude-web-ui
```

#### 方式二：让 Claude Code 自己装 🎉

```bash
claude
```

进入交互模式后，把下面这段话丢给它：

```
帮我安装 claude-web-ui：pip install claude-web-ui，然后运行 claude-web 启动服务
```

> 💡 用 Claude Code 给 Claude Code 装一个 Web UI。

#### 方式三：从源码安装

```bash
git clone https://github.com/heng1234/claude-web.git
cd claude-web
pip install -e .
claude-web
```

### 运行

```bash
claude-web                    # 默认 127.0.0.1:8765
claude-web --port 9000        # 自定义端口
claude-web --open             # 启动后自动打开浏览器
claude-web --host 0.0.0.0     # 局域网共享
```

### 局域网共享（可选）

```bash
claude-web --host 0.0.0.0
```
⚠️ 别暴露到公网，本工具**没有鉴权**。

### 自定义端口

```bash
PORT=9000 python server.py
```

### 多窗口并行对话

直接在新浏览器标签页 / 窗口打开 `http://127.0.0.1:8765`，点「＋ 新会话」即可并行对话。每个标签页独立，互不干扰。

---

## 🔐 提交前敏感信息检查

仓库内置了 `.githooks/pre-commit`，会在 `git commit` 前扫描**已暂存文件**，重点拦截：

- 私钥块
- 常见平台 Token / API Key 模式
- 新增行里的 `api_key` / `token` / `password` / `secret` 这类疑似敏感赋值

首次 clone 后执行一次：

```bash
git config core.hooksPath .githooks
```

手动检查当前文件也可以：

```bash
python3 scripts/check_sensitive_info.py --paths server.py static/index.html README.md
```

如果 hook 拦截了提交，先用 `git diff --cached` 看暂存内容，再决定是否移除或替换敏感信息。

---

## 🧩 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.9+ · FastAPI · uvicorn · SQLite · pypdf · python-docx |
| 前端 | 原生 JS · TailwindCSS · marked.js · highlight.js · Mermaid · KaTeX · Chart.js |
| 协议 | Server-Sent Events（流式输出）· stream-json stdin（多模态图片输入） |
| 依赖 | `claude` CLI（透过 subprocess 调用） |

## 📐 架构

```
浏览器 ──POST /api/chat──> FastAPI
                            └─ subprocess: claude -p [message | --input-format stream-json] \
                                           --output-format stream-json \
                                           --include-partial-messages \
                                           [--session-id | --resume] \
                                           [--permission-mode | --allowed-tools]
                               └─ stdout JSON lines ──SSE──> 浏览器渲染
```

- 会话 ID 首轮前端生成 UUID 通过 `--session-id` 传入，后续用 `--resume`
- `--include-partial-messages` 开启 token 级流式
- **图片输入**走 `--input-format stream-json` + stdin，base64 内联（跟 Anthropic API 多模态格式一致），而非通过 Read 工具间接读取，避免精度损失
- 每轮对话前若 cwd 是 git 仓库，执行 `git stash create` 创建 checkpoint
- 会话元数据存 SQLite（`claude-web.db`），事件流存 JSONL（`history/{session_id}.jsonl`）

---

## 📁 项目结构

```
claude-web/
├── server.py              # FastAPI 主服务
├── static/
│   └── index.html         # 单页前端
├── requirements.txt
├── screenshots/           # README 使用的截图
├── history/               # 会话事件 JSONL（运行时生成，gitignore）
├── uploads/               # 上传的图片/文档（运行时生成，gitignore）
├── claude-web.db          # SQLite 会话元数据（运行时生成，gitignore）
└── .venv/                 # 虚拟环境（gitignore）
```

---

## 🧰 API 端点速查

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/chat` | POST | 主对话，SSE 流式返回 |
| `/api/chat/stop/{session_id}` | POST | 停止正在运行的会话 |
| `/api/upload` | POST | 上传图片 |
| `/api/upload-doc` | POST | 上传文档（PDF / DOCX / CSV / TSV / TXT / MD / JSON / LOG），自动提取文本 |
| `/api/exec-code` | POST | 运行代码块（Python/JS/Bash，15s 超时） |
| `/api/fetch-url` | POST | 抓取 URL 正文（带 SSRF 防护） |
| `/api/sessions` | GET | 列出所有会话（含搜索 / 归档 / 标签） |
| `/api/sessions/{id}` | GET/PATCH/DELETE | 查看 / 修改 / 删除会话 |
| `/api/sessions/{id}/export` | GET | 导出 Markdown |
| `/api/sessions/{id}/prepare-fork` | POST | 创建分叉会话 |
| `/api/sessions/{id}/prepare-inline-edit` | POST | 就地编辑历史消息并从该轮继续 |
| `/api/sessions/{id}/restore-checkpoint` | POST | Git 回滚 |
| `/api/sessions/{id}/suggest-title` | POST | AI 智能命名 |
| `/api/suggest-followups` | POST | 生成跟进建议 |
| `/api/prompts` | GET/POST/PUT/DELETE | 提示词模板 CRUD |
| `/api/stats` | GET | 使用统计（成本 / 工具 / 每日） |
| `/api/git` | GET | 当前目录 git 状态 |
| `/api/files` | GET | 当前目录文件列表（@ 引用用） |
| `/api/cwds` | GET | 最近用过的工作目录 |
| `/api/tags` | GET | 所有标签统计 |

---

## ⚠️ 已知限制

- **权限审批是"失败后重试"模式**：不是 Cursor 那种执行中实时弹窗。当 Claude 因权限被拒时，UI 会显示琥珀色提示卡片，用户点击「允许 xxx 并重试」后系统自动把工具加入白名单并重发本轮。体验上多等一轮（20-30 秒），但功能完整。真正的实时交互审批需自建 MCP 权限服务器或双向 stream-json 通信。
- **Checkpoint 仅限 git 仓库**：非 git 目录跳过（后续可加 tar 快照）。
- **分叉会话会打包上下文**：编辑/重新生成时把历史作为前缀发给 Claude（可能多消耗 token）。
- **无鉴权**：仅供本地使用，不建议直接暴露公网。
- **代码块运行无沙盒**：Python/JS/Bash 直接在本机跑，点运行前会二次确认。
- **Claude CLI `-p` 模式流式局限**：CLI 在非交互模式下会整段缓冲后一次性吐事件，token 流式是"视觉动画模拟"，非真实 token 速率。

---

## 🗺 Roadmap

### 当前实现情况

#### 已实现 ✅
- [x] 多轮对话、停止生成、图片输入、工具调用可视化、思考过程展示
- [x] 文档上传（PDF / DOCX / CSV / TSV / TXT / MD / JSON / LOG）和 URL 自动抓取上下文
- [x] 联网搜索开关、`@` 文件引用、Token 估算、草稿自动保存、提示词模板库
- [x] Mermaid / LaTeX 渲染、图片 Lightbox、代码块复制与本地运行
- [x] 会话置顶 / 归档 / 标签 / 搜索 / 导出 / 双击重命名 / AI 智能命名
- [x] 历史消息就地编辑继续、重新生成分叉、Git checkpoint 回滚
- [x] TodoWrite 实时看板、统计面板、Git 状态栏、暗黑模式、移动端侧栏
- [x] 权限策略（CLI 默认 / 允许编辑 / 计划 / 只读 / 自定义工具）和权限失败后的本会话临时放行重试

#### 部分实现 / 有限制 ⚠️
- [ ] 运行时交互式权限审批仍未实现；目前是预设策略 + 权限失败后重试
- [ ] 真正按模型原始速率的 token 级流式输出仍受 Claude CLI `-p` 模式限制
- [ ] Git checkpoint / 回滚仅适用于 git 仓库
- [ ] 代码块运行已实现，但无沙盒，只适合本机可信代码

### 待办
- [ ] Artifacts 侧边预览（HTML/React 实时渲染）
- [ ] MCP server 管理面板
- [ ] Slash 命令透传（`/compact` `/clear` `/init`）
- [ ] 导入 `~/.claude/projects/` 原生会话
- [ ] 基于 MCP 的真·交互审批
- [ ] 简单鉴权（Token / 密码）
- [ ] 内嵌终端（xterm.js）
- [ ] Projects 分组（跨会话共享上下文）
- [ ] 划选文字浮动工具栏
- [ ] 语音输入 / 朗读

---

## 🤝 贡献

欢迎 Issue / PR。

## 📄 License

Apache License 2.0 — 见 [LICENSE](LICENSE)

## 🙏 致谢

- [Claude Code](https://docs.claude.com/claude-code) — Anthropic
- [FastAPI](https://fastapi.tiangolo.com/) · [TailwindCSS](https://tailwindcss.com/) · [marked](https://github.com/markedjs/marked) · [pypdf](https://github.com/py-pdf/pypdf) · [python-docx](https://github.com/python-openxml/python-docx)
