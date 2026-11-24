# 个人邮件助手

在一个桌面窗口里查看和回复 QQ、网易 163、126、Yeah、Gmail、iCloud 和 Yahoo 邮件。绑定邮箱即可使用，AI 助手默认关闭。

**[下载 Mac 桌面版（DMG）](https://github.com/zhengye123188/personal-mail-assistant/releases/latest/download/PersonalMailAssistant-macOS-arm64.dmg)** · 适用于 Apple 芯片和 macOS 26 或更高版本。[查看版本记录](https://github.com/zhengye123188/personal-mail-assistant/releases)

## 项目功能

- 在**全部收件箱**按时间查看来信，也可以切换到单个账号。支持筛选未读和旗标邮件，以及按发件人、主题或摘要搜索已载入的邮件。
- 标准版在打开 HTML 邮件时启用内置浏览器，按来信的样式显示图片、表格和链接按钮。外部图片默认自动载入，点击正文中的网页链接会打开系统浏览器。
- 重启后先显示本机缓存的邮件列表，再后台查收新邮件。已缓存的正文也可离线阅读。
- 应用运行时，带旗标或含「请确认」「请回复」等提示的新邮件会触发**待处理提醒**。手写和 AI 起草的回复都会自动保存到本机草稿箱，重启后可继续编辑。
- 开启 **AI 助手**后，可以用自然语言跨邮箱查信、总结来信，或用中文告诉 AI 想怎么回。生成的回复默认跟随来信语言，由你核对后发送。

## 使用方法

### 1. 安装并启动

**macOS 桌面应用**　打开下载的 DMG，把「个人邮件助手」拖到「应用程序」，再从「应用程序」中双击打开。安装包自带 Python 和图文阅读器。已使用源码版的用户，先正常退出旧窗口，再打开桌面版即可继续使用原有账号和草稿。

此版本采用本地签名，尚未经过 Apple 公证。若首次打开时提示无法验证开发者，确认安装包来自本仓库后，可在「系统设置 → 隐私与安全性」中点「仍要打开」。[Apple 官方说明](https://support.apple.com/zh-cn/102445)

**从源码运行**　建议使用 Python 3.11 或 3.12 和图形桌面环境。先克隆仓库并进入项目目录。

```sh
git clone https://github.com/zhengye123188/personal-mail-assistant.git
cd personal-mail-assistant
```

**macOS / Linux**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m maildesk
```

**Windows（PowerShell）**

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m maildesk
```

macOS 完成安装后，也可以双击 [启动邮件助手.command](./启动邮件助手.command)。Linux 绑定邮箱需要可用且已解锁的 Secret Service 凭据库。

要在 Mac 上用当前源码重新生成桌面应用，完成上述依赖安装后，在项目目录执行以下命令。成品位于 `dist/个人邮件助手.app`。

```sh
.venv/bin/python -m pip install 'PyInstaller==6.22.3'
.venv/bin/python -m PyInstaller --distpath dist packaging/macos.spec
```

### 2. 绑定邮箱

点左下角「添加邮箱账户」。例如绑定 `name@gmail.com`，在账号框输入 `name`，右侧选择 `@gmail.com`。也可以直接粘贴完整地址。

- 绑定 **QQ 或网易邮箱**时，在邮箱网页版开启 IMAP/SMTP 服务，生成客户端授权码。
- 绑定 **Gmail** 时，为 Google 账号开启两步验证，生成 16 位应用专用密码。此方式适用于可创建应用专用密码的账号。[Google 应用专用密码入口](https://myaccount.google.com/apppasswords)
- 绑定 **iCloud 或 Yahoo** 时，在对应账号的安全设置中，为邮件助手生成 App 专用密码或应用密码。

绑定窗口提供对应服务商的官方设置说明。填入凭据后点「绑定邮箱」，再按同样的步骤添加其他账号。

macOS 版启动时会自动同步已获授权的邮箱。某个邮箱需要授权时，收件箱会显示该邮箱地址。点「授权并记住」，在系统弹窗输入 Mac 登录密码，用鼠标点「始终允许」。桌面版会在系统钥匙串中记住该邮箱的授权码，供以后自动同步。系统首次确认旧凭据的访问权限和应用身份时，可能会连续询问两次。你也可以稍后授权，先阅读已缓存的邮件。[Apple 钥匙串授权说明](https://developer.apple.com/documentation/security/access-control-lists)

### 3. 读信、回复和使用 AI

在左侧选择账号或「全部收件箱」，点开邮件后选「回复」。写到一半可以关闭窗口，下次双击「草稿箱」中的邮件继续编辑。核对收件人和正文后点「发送回复」。

需要 AI 时，打开「AI 设置…」，勾选「启用邮箱 AI 助手」，选择 DeepSeek 或阿里云百炼（中国内地）的模型，填入 API 密钥并保存。所选云端模型会使用相关邮件节选处理你的请求。

- 要**查信**，点侧栏「邮箱助手」，例如问「上周有哪些邮件需要我确认时间？」点击回答中的来源可打开原邮件。
- 要**起草回复**，在「告诉 AI 怎么回」中直接输入你的要求，或按 **Tab** 采纳根据当前邮件生成的灰色建议。可以继续编辑，再点「按要求起草」。本地建议打开回复框即可使用，勾选「模型补全」还可以让所选模型结合来信续写你输入的开头。
