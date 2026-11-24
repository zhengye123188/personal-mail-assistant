<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./docs/brand/maildesk-header-dark.svg">
    <img src="./docs/brand/maildesk-header-light.svg" width="480" alt="MailDesk · 个人邮件助手">
  </picture>
</p>

<h3 align="center">多个邮箱，一个收件箱。</h3>

<p align="center">
  用个人邮件助手收发 QQ、网易、Gmail、iCloud 和 Yahoo 邮件。<br>
  需要时开启 AI，查找来信、起草回复，或把邮件里的截止日期加入待办。
</p>

<p align="center">
  <a href="https://github.com/zhengye123188/personal-mail-assistant/releases/tag/v0.2.0"><img src="https://img.shields.io/badge/release-v0.2.0-2453E8?style=flat-square" alt="查看 v0.2.0 发布说明"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-475569?style=flat-square" alt="MIT 许可证"></a>
  <a href="#下载安装包"><img src="https://img.shields.io/badge/download-macOS%20%2F%20Windows-35618A?style=flat-square" alt="下载 macOS 或 Windows 安装包"></a>
</p>

<p align="center"><strong>简体中文</strong> · <a href="./README.en.md" lang="en">English</a></p>

## 下载安装包

| 电脑 | 系统要求 | 安装包 |
| --- | --- | --- |
| Mac · Apple 芯片（M 系列） | macOS 26 或更高版本 | [下载 Apple 芯片版 DMG](https://github.com/zhengye123188/personal-mail-assistant/releases/download/v0.2.0/PersonalMailAssistant-macOS-arm64.dmg) |
| Mac · Intel 处理器 | macOS 26 或更高版本 | [下载 Intel 版 DMG](https://github.com/zhengye123188/personal-mail-assistant/releases/download/v0.2.0/PersonalMailAssistant-macOS-x86_64.dmg) |
| Windows · 64 位 Intel 或 AMD 处理器 | Windows 10（1809 或更高版本）或 Windows 11 | [下载 Windows x64 安装程序](https://github.com/zhengye123188/personal-mail-assistant/releases/download/v0.2.0/PersonalMailAssistant-Windows-x64-Setup.exe) |

Mac 用户可在苹果菜单的「关于本机」查看芯片或处理器。[查看版本记录](https://github.com/zhengye123188/personal-mail-assistant/releases)

## 项目功能

- 在**全部收件箱**按时间查看来信，也可以切换到单个账号。支持筛选未读和旗标邮件，以及按发件人、主题或摘要搜索已载入的邮件。
- 标准版在打开 HTML 邮件时启用内置浏览器，按来信的样式显示图片、表格和链接按钮。外部图片默认自动载入，点击正文中的网页链接会打开系统浏览器。
- 重启后先显示本机缓存的邮件列表，再后台查收新邮件。已缓存的正文也可离线阅读。
- 应用运行时，带旗标或含「请确认」「请回复」等提示的新邮件会触发**待处理提醒**。手写和 AI 起草的回复都会自动保存到本机草稿箱，重启后可继续编辑。
- 开启 **AI 助手**后，可以用自然语言跨邮箱查信、总结来信，或用中文告诉 AI 想怎么回。生成的回复默认跟随来信语言，由你核对后发送。
- 在独立的「邮箱助手」窗口里可以接着追问某封信，从来信中提取截止日期，并把核对过的事项加入本机待办。侧栏的「截止待办」可直接查看原信或标记完成。

## 使用方法

### 1. 安装并启动

**macOS 桌面应用**　打开下载的 DMG，把「个人邮件助手」拖到「应用程序」，再从「应用程序」中双击打开。安装包自带 Python 和图文阅读器。已使用源码版的用户，先正常退出旧窗口，再打开桌面版即可继续使用原有账号和草稿。

此版本采用本地签名，尚未经过 Apple 公证。若首次打开时提示无法验证开发者，确认安装包来自本仓库后，可在「系统设置 → 隐私与安全性」中点「仍要打开」。[Apple 官方说明](https://support.apple.com/zh-cn/102445)

**Windows 桌面应用**　运行上方的 x64 安装程序，按向导安装到当前用户。完成后从「开始」菜单打开「个人邮件助手」。安装包自带运行环境，当前版本未做 Windows 代码签名。

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

- 绑定 **QQ 或网易邮箱（163、126、Yeah）**时，在邮箱网页版开启 IMAP/SMTP 服务，生成客户端授权码。
- 绑定 **Gmail** 时，为 Google 账号开启两步验证，生成 16 位应用专用密码。此方式适用于可创建应用专用密码的账号。[Google 应用专用密码入口](https://myaccount.google.com/apppasswords)
- 绑定 **iCloud 或 Yahoo** 时，在对应账号的安全设置中，为邮件助手生成 App 专用密码或应用密码。

绑定窗口提供对应服务商的官方设置说明。填入凭据后点「绑定邮箱」，再按同样的步骤添加其他账号。

macOS 版启动时会自动同步已获授权的邮箱。某个邮箱需要授权时，收件箱会显示该邮箱地址。点「授权并记住」，在系统弹窗输入 Mac 登录密码，用鼠标点「始终允许」。桌面版会在系统钥匙串中记住该邮箱的授权码，供以后自动同步。系统首次确认旧凭据的访问权限和应用身份时，可能会连续询问两次。你也可以稍后授权，先阅读已缓存的邮件。[Apple 钥匙串授权说明](https://developer.apple.com/documentation/security/access-control-lists)

### 3. 读信、回复和使用 AI

在左侧选择账号或「全部收件箱」，点开邮件后选「回复」。写到一半可以关闭窗口，下次双击「草稿箱」中的邮件继续编辑。核对收件人和正文后点「发送回复」。

AI 默认关闭。需要时，打开「AI 设置…」，勾选「启用邮箱 AI 助手」，选择 DeepSeek 或阿里云百炼（中国内地）的模型，填入 API 密钥并保存。所选云端模型会使用相关邮件节选处理你的请求。

- 要**查信**，点侧栏「邮箱助手」，例如问「上周有哪些邮件需要我确认时间？」点击回答中的来源可打开原邮件。
- 要**起草回复**，在「告诉 AI 怎么回」中直接输入你的要求，或按 **Tab** 采纳根据当前邮件生成的灰色建议。可以继续编辑，再点「按要求起草」。本地建议打开回复框即可使用，勾选「模型补全」还可以让所选模型结合来信续写你输入的开头。

### 4. 在助手里跟进一件事

桌面版 v0.2.0 已包含历史对话侧栏、截止待办和用户偏好。Mac 用户升级时先退出旧版，再把新版拖入「应用程序」并替换。若某个邮箱提示需要授权，点「授权并记住」完成系统确认。

1. 点侧栏「邮箱助手」，选择一个邮箱或「全部收件箱」。可以先问「找出过去一周与报名有关的邮件」，再问「总结第二封，我要交什么材料、最晚什么时候交？」回答中的来源可以点回原信。
2. 问「把有明确截止日期的事项列为待办建议」。逐项核对引用原文、事项和本地截止时间，必要时调整，再点「确认并添加提醒」。原文缺少时区时按本机时间理解，只有日期时会注明预填当天 23:59。
3. 在「偏好」中设置回答语言、详略、优先关注的话题和提前提醒分钟数。比如把「审稿和需要我确认时间的会议」填入「优先关注」，今后总结来信时助手会参考这个偏好。
4. 应用运行时每分钟检查一次已确认的待办，默认提前 30 分钟弹窗。退出期间错过的提醒会在下次启动时补发。关闭 AI 后仍可在侧栏查看、完成待办并接收提醒。

「对话」页左侧列出当前邮箱范围的历史对话，最近聊过的排在最上面。点标题可继续聊天，点「新建对话」可另起话题，第一轮回答完成后会以首个问题命名。窗口较窄时，点「历史对话」打开列表，选中一条即可回到聊天。

点某行右侧的垃圾桶并确认，即可删除这段对话。已确认的截止待办和已保存的邮件草稿仍可在「截止待办」和草稿箱查看。单段对话超过 8 轮时，可点「更早记录」翻阅之前的问答。

聊天问答及邮件来源索引保存在本机 SQLite 中。升级前已有的问答节选会按原来的邮箱范围各自归入一段会话。每次提问最多带入当前对话最近 4 轮的节选，助手按需调用查信、读正文、列待办、提议提醒和起草工具。后台截止提醒直接读取本机待办。

## 许可证

本项目的原创代码和文档采用 MIT 许可证，允许修改、商用和分发。复制或分发全部或实质性部分时，请保留版权声明和许可声明。[MIT 许可证全文](./LICENSE)

桌面安装包中的第三方组件各自适用上游许可。邮件阅读器使用 Qt WebEngine 和 Chromium，v0.2.0 的 Apple 芯片版还包含 Qt Charts。[Qt WebEngine 与 Chromium 许可](https://doc.qt.io/qt-6/qtwebengine-licensing.html) · [Qt Charts 许可](https://doc.qt.io/qt-6/qtcharts-index.html#licenses)
