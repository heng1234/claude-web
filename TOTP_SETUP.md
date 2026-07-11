![img.png](img.png)# TOTP Authenticator 设置指南

## Linux 服务器部署场景

当你将 claude-web 部署到 Linux 服务器时，可以使用命令行工具直接在服务器上设置 TOTP 认证。

## 使用方法

### 1. SSH 登录到服务器

```bash
ssh user@your-server.com
cd /path/to/claude-web
```

### 2. 运行 TOTP 设置命令

```bash
python server.py --setup-totp
```

或者如果使用虚拟环境：

```bash
.venv/bin/python server.py --setup-totp
```

### 3. 按照提示操作

命令会显示：
- 终端二维码（如果安装了 qrcode 库）
- 手动输入的密钥（Secret）
- 账户名称和发行者信息

### 4. 用手机扫描二维码

使用以下任一 Authenticator 应用扫描：
- Google Authenticator
- Microsoft Authenticator
- Authy
- 1Password
- 其他支持 TOTP 的应用

### 5. 输入验证码

从 Authenticator 应用中输入当前显示的 6 位验证码以完成设置。

## 示例输出

```
============================================================
  TOTP Authenticator Setup
============================================================

1. Open your authenticator app (Google Authenticator, Authy, etc.)
2. Scan the QR code below, or manually enter the secret

[二维码显示在这里]

Account:  your-server-hostname
Secret:   
Issuer:   Claude Code Web

3. Enter the 6-digit code from your authenticator to verify:
   Code: 123456
```

## 重新设置

如果需要重新生成 TOTP 密钥，再次运行命令：

```bash
python server.py --setup-totp
```

系统会提示是否禁用当前配置并生成新密钥。

## 注意事项

1. **密钥安全**：生成的密钥会保存在数据库中，请确保服务器安全
2. **备份**：设置完成后，建议在 Authenticator 应用中备份
3. **时间同步**：确保服务器时间准确（TOTP 依赖时间戳）
4. **访问权限**：设置后，远程访问将需要 Authenticator 验证码，随机访问码会被禁用

## 依赖

qrcode 库用于在终端显示二维码：

```bash
pip install 'qrcode[pil]'
```

如果未安装，命令仍可运行，但会显示手动输入的密钥而非二维码。
