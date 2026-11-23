# 个人邮件助手

一款轻量的本地桌面邮件助手。在一个窗口里查看和回复 QQ、网易 163、126、Yeah、Gmail、iCloud 和 Yahoo 邮件。绑定邮箱即可使用，AI 助手默认关闭。

## 项目功能

- 在**全部收件箱**按时间查看来信，也可以切换到单个账号。支持筛选未读和旗标邮件，以及按发件人、主题或摘要搜索已载入的邮件。
- 用内置阅读器查看邮件中的文字、图片、表格和链接。外部图片默认自动载入，链接在浏览器中打开。复杂的网页样式会按阅读器支持的格式显示。
- 重启后先显示本机缓存的邮件列表，再后台查收新邮件。已缓存的正文也可离线阅读。
- 应用运行时，带旗标或含「请确认」「请回复」等提示的新邮件会触发**待处理提醒**。手写和 AI 起草的回复都会自动保存到本机草稿箱，重启后可继续编辑。
- 开启 **AI 助手**后，可以用自然语言跨邮箱查信、总结来信，或用中文告诉 AI 想怎么回。生成的回复默认跟随来信语言，由你核对后发送。

## 使用方法

### 1. 安装并启动

建议使用 Python 3.11 和图形桌面环境。先克隆仓库并进入项目目录。

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

### 2. 绑定邮箱

点左下角「添加邮箱账户」。例如绑定 `name@gmail.com`，在账号框输入 `name`，右侧选择 `@gmail.com`。也可以直接粘贴完整地址。

- 绑定 **QQ 或网易邮箱**时，在邮箱网页版开启 IMAP/SMTP 服务，生成客户端授权码。
- 绑定 **Gmail** 时，为 Google 账号开启两步验证，生成 16 位应用专用密码。此方式适用于可创建应用专用密码的账号。[Google 应用专用密码入口](https://myaccount.google.com/apppasswords)
- 绑定 **iCloud 或 Yahoo** 时，在对应账号的安全设置中，为邮件助手生成 App 专用密码或应用密码。

绑定窗口提供对应服务商的官方设置说明。填入凭据后点「绑定邮箱」，再按同样的步骤添加其他账号。

### 3. 读信、回复和使用 AI

在左侧选择账号或「全部收件箱」，点开邮件后选「回复」。写到一半可以关闭窗口，下次双击「草稿箱」中的邮件继续编辑。核对收件人和正文后点「发送回复」。

需要 AI 时，打开「AI 设置…」，勾选「启用邮箱 AI 助手」，选择 DeepSeek 或阿里云百炼（中国内地）的模型，填入 API 密钥并保存。所选云端模型会使用相关邮件节选处理你的请求。

- 要**查信**，点侧栏「邮箱助手」，例如问「上周有哪些邮件需要我确认时间？」点击回答中的来源可打开原邮件。
- 要**起草回复**，在回复框的「告诉 AI 怎么回」中输入「周三下午不方便，问对方能否改到周五上午」，点「按要求起草」。想让模型结合来信续写你的要求，可勾选「模型补全」，出现灰色建议时按 **Tab** 采纳。
