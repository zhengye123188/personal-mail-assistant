<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/zhengye123188/personal-mail-assistant/81267d5be9c2f538095c9ad7ab69d658a2146ace/docs/brand/maildesk-header-dark.svg">
    <img src="https://raw.githubusercontent.com/zhengye123188/personal-mail-assistant/81267d5be9c2f538095c9ad7ab69d658a2146ace/docs/brand/maildesk-header-light.svg" width="480" alt="MailDesk · Personal Mail Assistant">
  </picture>
</p>

<h3 align="center">One inbox for all your accounts.</h3>

<p align="center">
  Read and reply to QQ, NetEase, Gmail, iCloud and Yahoo mail in Personal Mail Assistant.<br>
  Turn on AI when you want help finding a message, drafting a reply or tracking a deadline.
</p>

<p align="center">
  <a href="https://github.com/zhengye123188/personal-mail-assistant/releases/tag/v0.2.0"><img src="https://raw.githubusercontent.com/zhengye123188/personal-mail-assistant/81267d5be9c2f538095c9ad7ab69d658a2146ace/docs/brand/badge-release.png" width="100" height="20" alt="View the v0.2.0 release"></a>
  <a href="./LICENSE"><img src="https://raw.githubusercontent.com/zhengye123188/personal-mail-assistant/81267d5be9c2f538095c9ad7ab69d658a2146ace/docs/brand/badge-license.png" width="83" height="20" alt="MIT License"></a>
  <a href="#download-an-installer"><img src="https://raw.githubusercontent.com/zhengye123188/personal-mail-assistant/81267d5be9c2f538095c9ad7ab69d658a2146ace/docs/brand/badge-download.png" width="179" height="20" alt="Download a macOS or Windows installer"></a>
</p>

<p align="center"><a href="./README.md" lang="zh-CN">简体中文</a> · <strong>English</strong></p>

## Download an installer

| Computer | System requirements | Installer |
| --- | --- | --- |
| Mac with Apple silicon (M series) | macOS 26 or later | [Download the Apple silicon DMG](https://github.com/zhengye123188/personal-mail-assistant/releases/download/v0.2.0/PersonalMailAssistant-macOS-arm64.dmg) |
| Intel Mac | macOS 26 or later | [Download the Intel DMG](https://github.com/zhengye123188/personal-mail-assistant/releases/download/v0.2.0/PersonalMailAssistant-macOS-x86_64.dmg) |
| Windows PC with a 64-bit Intel or AMD processor | Windows 10 version 1809 or later, or Windows 11 | [Download the Windows x64 installer](https://github.com/zhengye123188/personal-mail-assistant/releases/download/v0.2.0/PersonalMailAssistant-Windows-x64-Setup.exe) |

On a Mac, open **Apple menu → About This Mac** to check your chip or processor. [All releases](https://github.com/zhengye123188/personal-mail-assistant/releases)

## Features

- Read messages from all inboxes in date order or switch to one account. Filter unread or flagged messages, or search loaded messages by sender, subject, or preview.
- Read formatted HTML email with images, tables, and linked buttons. The embedded browser loads when you open an HTML message. Remote images load by default, and web links open in your system browser.
- See the cached message list as soon as you reopen the app while it checks for new mail in the background. You can also read previously cached messages offline.
- Get a desktop alert while the app is running when a new message is flagged or contains a phrase such as “请确认” (please confirm) or “请回复” (please reply). Replies you write or draft with AI are automatically saved in local drafts so you can finish them later.
- Ask the optional AI assistant to find mail across accounts, summarize a message, or draft a reply from your instructions. You can give instructions in Chinese and the draft will default to the language of the incoming message. You review and send it.
- Follow up on a message in its own conversation, extract a deadline, and turn it into a local reminder after checking the original email. Open the source message or mark the task complete from the sidebar.

## Install and launch

**macOS.** Open the DMG, drag **个人邮件助手** into **Applications**, then launch it from there. Python and the HTML reader are included. When upgrading, quit the previous version before replacing the app. If you previously ran from source, quit that instance first. The desktop app can use your existing accounts and drafts.

**If Apple cannot verify the app on first launch.** The v0.2.0 Mac app is locally signed and has not been notarized by Apple. If you downloaded the installer from this repository’s GitHub Release and trust this version, follow these steps.

1. In the alert with **Done** and **Move to Trash**, click **Done** to keep the app.
2. Open **System Settings → Privacy & Security**. Scroll down to **Security** and click **Open Anyway** next to **个人邮件助手**.
3. Authenticate if prompted, then click **Open** in the confirmation dialog. macOS remembers this choice for the app, so you can open it from **Applications** as usual next time.

If **Open Anyway** is missing, double-click **个人邮件助手** in **Applications** again, dismiss the alert, and return to **Privacy & Security**. [Apple’s instructions](https://support.apple.com/en-us/102445)

**Windows.** Run the x64 installer, follow the prompts, then open **个人邮件助手** from the Start menu. The installer sets up the app for the current user and includes its runtime. The Windows installer is unsigned.

### Run from source

Use Python 3.11 or 3.12 in a desktop environment. Clone the repository and enter the project directory.

```sh
git clone https://github.com/zhengye123188/personal-mail-assistant.git
cd personal-mail-assistant
```

**macOS and Linux**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m maildesk
```

**Windows (PowerShell)**

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m maildesk
```

On macOS, after setup you can also double-click the [launcher script](./启动邮件助手.command). On Linux, connecting an account requires an unlocked credential store that supports the Secret Service API.

To build the Mac app from source, install the dependencies above and run the following from the project directory. The app will be at `dist/个人邮件助手.app`.

```sh
.venv/bin/python -m pip install 'PyInstaller==6.22.3'
.venv/bin/python -m PyInstaller --distpath dist packaging/macos.spec
```

## Connect an email account

Click **添加邮箱账户** (Add email account) in the lower-left corner. For an address like `name@gmail.com`, enter `name` and select `@gmail.com` from the menu, or paste the full address.

- **QQ and NetEase (163, 126, and Yeah).** Enable IMAP/SMTP in your provider’s webmail settings and generate a client authorization code.
- **Gmail.** Enable 2-Step Verification and generate a 16-character app password if your Google account supports app passwords. [Google App Passwords](https://myaccount.google.com/apppasswords)
- **iCloud and Yahoo.** Generate an app-specific password in your account’s security settings.

The connection dialog links to each provider’s official setup instructions. Enter the code or app password, click **绑定邮箱** (Connect account), and repeat for any other accounts.

On macOS, authorized accounts sync automatically at startup. If an account needs permission, its address appears in the inbox. Click **授权并记住** (Authorize and remember), enter your Mac login password in the system prompt, and click **Always Allow**. The app uses Keychain to remember the mail credential. macOS may prompt twice the first time it checks access to an existing credential and verifies the app’s identity. You can read cached mail and authorize the account later. [Apple Keychain access controls](https://developer.apple.com/documentation/security/access-control-lists)

## Read, reply, and use AI

Select an account or **全部收件箱** (All inboxes), open a message, and click **回复** (Reply). To resume an unfinished reply, double-click it in **草稿箱** (Drafts). Check the recipient and reply text before clicking **发送回复** (Send reply).

AI features are off by default. To turn them on, open **AI 设置…** (AI settings) and select **启用邮箱 AI 助手** (Enable email AI assistant). Choose a model from DeepSeek or Alibaba Cloud Model Studio (Chinese mainland), enter your API key, and save. The selected cloud model receives the relevant email excerpts when it processes your requests.

- **Find mail.** Open **邮箱助手** (Mail assistant) in the sidebar and ask, for example, “Which emails from last week asked me to confirm a time?” Click a source in the answer to open the original message.
- **Draft a reply.** In **告诉 AI 怎么回** (Tell AI how to reply), enter your instructions or press **Tab** to accept the gray suggestion based on the incoming email. Edit the instructions, then click **按要求起草** (Draft from instructions). Local suggestions are available as soon as the reply window opens. Enable **模型补全** (Model completion) to have the selected model continue what you’ve started typing using the email as context.

## Follow up on a conversation or deadline

1. Open **邮箱助手** and select an account or **全部收件箱**. Ask “Find registration emails from the last week,” then follow up with “Summarize the second email. What do I need to submit and by when?” Use the sources in the response to check the original message.
2. Ask the assistant to suggest tasks with explicit deadlines. Review the quoted evidence, task, and local due time, then click **确认并添加提醒** (Confirm and add reminder). Dates without a time zone use your local time. When the email gives only a date, the assistant flags the prefilled time of 23:59 so you can adjust it.
3. Open **偏好** (Preferences) to set the response language, level of detail, topics to prioritize, and reminder lead time. For example, enter “Peer reviews and meetings where I need to confirm a time” under **优先关注** and the assistant will take that into account when summarizing mail.
4. The app checks confirmed tasks every minute while running. By default, it shows an alert 30 minutes before the deadline. Missed reminders appear the next time you launch the app. You can still view and complete tasks under **截止待办** (Deadline tasks) and receive reminders after turning off AI.

The **对话** (Chat) sidebar shows conversations for the selected inbox or group of inboxes, most recently active first. Click a title to continue or **新建对话** (New chat) to start another topic. The first question becomes the title once the assistant answers. In a narrow window, open **历史对话** (Chat history) to switch conversations.

To delete a conversation, click the trash icon next to its title and confirm. Confirmed tasks and saved email drafts remain available in their respective folders. For conversations longer than eight rounds, click **更早记录** (Earlier messages) to read the previous exchanges.

Conversations and references to source emails are stored locally in SQLite. When you upgrade, earlier saved conversation excerpts are grouped by the inboxes they belonged to. Each new request can use excerpts from up to the last four exchanges in the active conversation. The assistant can search mail, read messages, list tasks, suggest reminders, and draft replies as needed. The background reminder check reads the local task list.

## License

The project's original code and documentation are licensed under the MIT License. You may modify, use commercially, and redistribute them. Include the copyright and permission notices when copying or distributing all or a substantial portion of the software. [Full MIT License](./LICENSE)

Third-party components in the desktop installers retain their upstream licenses. The mail reader uses Qt WebEngine and Chromium. The v0.2.0 Apple silicon build also includes Qt Charts. [Qt WebEngine and Chromium licensing](https://doc.qt.io/qt-6/qtwebengine-licensing.html) · [Qt Charts licensing](https://doc.qt.io/qt-6/qtcharts-index.html#licenses)
