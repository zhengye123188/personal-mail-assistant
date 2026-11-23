"""Connection settings and account setup instructions for supported personal mailboxes."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MailProvider:
    name: str
    domains: tuple[str, ...]
    imap_host: str
    smtp_host: str
    credential_label: str
    setup_hint: str
    help_url: str
    smtp_port: int = 465
    netease_id: bool = False


def _netease(name: str, domain: str) -> MailProvider:
    return MailProvider(
        name=name,
        domains=(domain,),
        imap_host=f"imap.{domain}",
        smtp_host=f"smtp.{domain}",
        credential_label="客户端授权码",
        setup_hint="在网易邮箱网页版的「设置 → POP/SMTP/IMAP」中开启 IMAP 服务并生成客户端授权码。",
        help_url=(
            "https://help.mail.126.com/faqDetail.do?"
            "code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2a5feb28b66796d3b"
        ),
        netease_id=True,
    )


MAIL_PROVIDERS: dict[str, MailProvider] = {
    "qq": MailProvider(
        name="QQ 邮箱",
        domains=("qq.com",),
        imap_host="imap.qq.com",
        smtp_host="smtp.qq.com",
        credential_label="客户端授权码",
        setup_hint="在 QQ 邮箱网页版的「设置 → 账号与安全」中开启 IMAP/SMTP 服务并生成授权码。",
        help_url="https://hiflow.tencent.com/docs/applications/qq-mail/",
    ),
    "163": _netease("网易 163 邮箱", "163.com"),
    "126": _netease("网易 126 邮箱", "126.com"),
    "yeah": _netease("网易 Yeah 邮箱", "yeah.net"),
    "gmail": MailProvider(
        name="Gmail",
        domains=("gmail.com", "googlemail.com"),
        imap_host="imap.gmail.com",
        smtp_host="smtp.gmail.com",
        credential_label="应用专用密码",
        setup_hint="在 Google 账号开启两步验证，生成 16 位应用专用密码。若账号策略禁用应用专用密码，此方式不可用。",
        help_url="https://support.google.com/accounts/answer/185833?hl=zh-Hans",
    ),
    "icloud": MailProvider(
        name="iCloud 邮箱",
        domains=("icloud.com", "me.com", "mac.com"),
        imap_host="imap.mail.me.com",
        smtp_host="smtp.mail.me.com",
        smtp_port=587,
        credential_label="App 专用密码",
        setup_hint="为 Apple 账户开启双重认证，在「登录和安全 → App 专用密码」生成密码，使用你的 iCloud 邮件地址绑定。",
        help_url="https://support.apple.com/zh-cn/102654",
    ),
    "yahoo": MailProvider(
        name="Yahoo 邮箱",
        domains=("yahoo.com",),
        imap_host="imap.mail.yahoo.com",
        smtp_host="smtp.mail.yahoo.com",
        credential_label="应用密码",
        setup_hint="在 Yahoo 账号安全页的「External connections」中选择「Create app password」，生成此应用的密码。",
        help_url="https://help.yahoo.com/kb/mail/learn-delete-password-create-sln15241.html",
    ),
}


_LOCAL_PART = re.compile(r"[a-z0-9_+%-]+(?:[.][a-z0-9_+%-]+)*", re.ASCII)


def provider_for_address(address: str) -> str | None:
    local, _, domain = address.strip().lower().partition("@")
    if len(local) > 64 or not _LOCAL_PART.fullmatch(local):
        return None
    return next((key for key, settings in MAIL_PROVIDERS.items() if domain in settings.domains), None)
