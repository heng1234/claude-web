# Claude Code Web 2.0 Authenticator 指南

本文对应 Claude Code Web `2.0.0`。

Authenticator 使用标准 TOTP 六位动态验证码，适合需要通过公网或非可信网络访问 Claude Code Web 的场景。本机和可信私有网络请求不要求验证码。

## 先了解访问范围

2.0 将以下来源视为本地 / 私有来源：

- `localhost`、`127.0.0.1`、`::1`
- `10.0.0.0/8`
- `172.16.0.0/12`
- `192.168.0.0/16`
- 链路本地地址
- 项目用于本地网络测试的保留网段

这些来源不会进入手机登录页。公网来源或反向代理转发的公网客户端需要访问码或 Authenticator。

## 推荐：在设置界面启用

1. 在电脑本机打开 Claude Code Web。
2. 进入「设置 → 手机访问」。
3. 开启手机访问。
4. 在「Authenticator 动态验证」区域点击「开始设置」。
5. 使用 Google Authenticator、Microsoft Authenticator、Authy、1Password 等应用扫描二维码，或手动输入 Secret。
6. 输入应用当前显示的六位验证码。
7. 点击「验证并启用」。

启用成功后：

- 公网来源改用 Authenticator 动态验证码登录。
- 原有随机访问码会被清除。
- 验证码约每 30 秒更新。
- 已使用的时间窗口不能重复登录，降低重放风险。
- 设备授权有效期仍由「手机访问」设置控制。

## 终端设置

服务器没有桌面浏览器时，可以在终端运行：

```bash
claude-web --setup-totp
```

源码运行：

```bash
.venv/bin/python server.py --setup-totp
```

终端会显示：

- TOTP 配置二维码（安装 `qrcode` 时）
- 手动输入 Secret
- Issuer：`Claude Code Web`
- 当前主机对应的账户名称
- 六位验证码输入提示

可选二维码依赖：

```bash
pip install 'qrcode[pil]'
```

未安装二维码依赖时仍可使用手动 Secret 完成设置。

## 远程部署建议

推荐拓扑：

```text
公网浏览器
    │ HTTPS
    ▼
Nginx / Caddy（TLS、限速）
    │ 转发到本机或私网
    ▼
Claude Code Web
```

建议：

- 使用 HTTPS，避免验证码和授权 Cookie 被窃听。
- 反向代理只把请求转发到 `127.0.0.1` 或明确的私网地址。
- 为 `/mobile-login` 和 `/api/mobile-access/login` 设置速率限制。
- 正确传递 `X-Forwarded-For` 和 `X-Forwarded-Proto`。
- 不要让公网直接访问 Claude Code Web 的监听端口。
- 不建议使用 `--host 0.0.0.0`；优先绑定明确地址。

Claude Code Web 只在直连节点已经属于本地 / 私网代理时信任转发头，公网客户端无法仅靠伪造 `X-Forwarded-For` 把自己标记成本地来源。

## 停用或重新配置

在「设置 → 手机访问」中：

- 点击停用 Authenticator 会清除当前 TOTP 配置。
- 停用后，所有远程授权设备会被撤销。
- 重新设置会生成新的 Secret，旧 Secret 立即失效。
- 可随时在“已授权设备”中撤销单个设备或全部撤销。

终端重新运行 `claude-web --setup-totp` 时，如果已经启用，会先询问是否禁用当前配置并生成新 Secret。

## 故障排查

### 本机打开时没有验证码页面

这是 2.0 的预期行为。本机和可信私网请求不要求验证码。

### 公网打开时没有验证码页面

检查：

1. 「设置 → 手机访问」是否已启用。
2. 反向代理是否把真实公网地址放入 `X-Forwarded-For`。
3. 是否错误地覆盖了客户端地址。
4. Claude Code Web 是否直接收到来自私网反向代理的请求。

### 验证码始终错误

- 确保服务器与手机时间自动同步。
- 确保输入的是当前六位验证码。
- 不要重复使用刚刚成功登录过的同一验证码。
- 如果 Secret 已重新生成，需要在 Authenticator 应用中删除旧账户并重新扫描。

### 按钮无法点击

- Authenticator 只能从本机 / 私网管理页面配置。
- 先开启「手机访问」，再点击「开始设置」。
- 刷新页面后确认后端版本为 `2.0.0`。

## Secret 与备份

- Secret 保存在本地 `claude-web.db`，不要提交数据库。
- 不要把二维码、Secret、登录 Cookie 或真实公网地址放入截图和 Issue。
- 如需截图，请遮盖二维码、Secret、设备标识、IP 和反向代理域名。
- 更换设备前，在 Authenticator 应用中完成安全备份，或重新生成 Secret。
