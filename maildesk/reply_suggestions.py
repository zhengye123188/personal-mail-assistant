"""Local writing suggestions derived from the current message's readable text."""

import re


_QUOTE = re.compile(r"^(?:>|-{2,}\s*(?:原始邮件|Original Message|Forwarded message)|"
                    r"On .+ wrote:|在.{1,160}写道[：:]|发件人[：:]|From:\s*\S+@)", re.I)
_NO_REPLY = re.compile(r"(?:请勿|无需|不要)(?:直接)?回复|(?:邮箱|信箱)无人值守|"
                       r"\b(?:do not reply|no reply (?:required|needed)|unmonitored (?:mailbox|address))\b", re.I)
_AUTH_CODE = re.compile(r"验证码|动态口令|一次性密码|验证代码|\b(?:verification code|login code|sign.in code|"
                        r"one.time (?:code|passcode|password))\b", re.I)
_FOOTER = re.compile(r"^--\s*$|^—\s*$|reply to this email directly|you (?:are receiving|received) this because|"
                     r"\bunsubscribe\b|点击退订|取消订阅", re.I | re.M)
_MINUTES = re.compile(r"(?:会议|评审|讨论|会后|面试).{0,4}(?:纪要|记录|回顾)|meeting minutes", re.I)
_DAY = r"(?:[上下本这]?周[一二三四五六日天]|星期[一二三四五六日天]|今天|明天|后天|\d{1,2}月\d{1,2}[日号])"
_CLOCK = r"(?:(?:凌晨|早上|上午|中午|下午|傍晚|晚上)\s*)?(?:[一二两三四五六七八九十\d]{1,3}点(?:半|[一二三四五六七八九十\d]{1,3}分)?|\d{1,2}[:：]\d{2})"
_WHEN = re.compile(rf"{_DAY}(?:\s*{_CLOCK}|早上|上午|下午|晚上)?|{_CLOCK}")
_EVENT = re.compile(r"读书会|设计评审|项目评审|评审会?|面试|组会|例会|研讨会|分享会|会议|培训|讲座")
_EARLY = re.compile(r"提前\s*[一二两三四五六七八九十半\d]+\s*(?:分钟|小时|个小时)")
_REQUEST = re.compile(r"(?:请(?:你|您)?|希望(?:你|您)|麻烦(?:你|您)?)(?:在[^。！？；，,\n]{1,18}(?:前|内))?(?:特别|重点|协助)?"
                      r"(?:留意|核对|审阅|审核|确认|提供|补充|提交|发送|发来|准备|查看)\s*([^。！？；，,\n]{2,38})")


def _latest_text(body):
    lines = []
    for line in body[:16000].splitlines():
        line = line.strip()
        if _QUOTE.match(line):
            break
        if line:
            lines.append(line)
    return "\n".join(lines)


def _label(text, limit=48):
    text = " ".join(text.split()).strip(" 「」“”。！？?!")
    if len(text) > limit:
        return text[:limit - 1] + "…"
    return text


def _event_suggestions(event, body, subject):
    changed = re.search(rf"(?:调整到|调整至|更改为|改到|改为|推迟到|提前到|延期至)\s*({_WHEN.pattern})", body)
    if changed:
        return (f"谢谢对方的提醒，告诉对方已收到{event}改到{changed[1]}的通知。",)
    if re.search(r"(?:会议|面试|评审|活动).{0,8}(?:已取消|取消了)|取消(?:了)?(?:本次|此次|这次)?(?:会议|面试|评审|活动)", body):
        return (f"谢谢对方的提醒，告诉对方已收到{event}取消的通知。",)
    time = _WHEN.search(body) or _WHEN.search(subject)
    occasion = (time[0] + "的" if time else "") + event
    early = _EARLY.search(body)
    if early:
        task = "帮忙布置" if "布置" in body else "到场"
        return (f"谢谢对方的邀请，问问{event}{early[0]}{task}需要做哪些准备。",
                f"告诉对方已收到{occasion}的安排，问问{early[0]}在哪里集合。")
    if time:
        return (f"谢谢对方的邀请，告诉对方已收到{occasion}安排。",
                f"询问{occasion}需要提前准备哪些材料。")
    return (f"谢谢对方的邀请，询问{event}的具体日期和时间。",
            f"询问参加{event}需要提前准备什么。")


def reply_suggestions(subject: str, body: str) -> tuple[str, ...]:
    """Return suggested instructions, ordered for Tab completion in this reply only."""
    subject = re.sub(r"^(?:(?:re|fw|fwd|回复|转发)\s*[:：]\s*)+", "", subject[:400].strip(), flags=re.I)
    if subject == "（无主题）":
        subject = ""
    body = _latest_text(body)
    context = subject + "\n" + body
    if not context.strip() or _NO_REPLY.search(context):
        return ()
    if _AUTH_CODE.search(context) and re.search(r"(?<!\d)\d{4,8}(?!\d)|[：:]\s*[A-Z0-9]{4,10}\b|请勿泄露", context):
        return ()
    # GitHub notification footers include both a reply route and an unsubscribe link.
    # Neither is the subject of the reply. Keep the PR title and the author's comment.
    body = _FOOTER.split(body, maxsplit=1)[0].strip()
    context = subject + "\n" + body

    # Prefer a concrete review or document request over a passing mention of a meeting.
    request = _REQUEST.search(body)
    if request and not any(word in request[0] for word in ("是否", "时间", "时段", "方便")):
        detail = _label(request[1])
        if "校样" in context or re.search(r"审阅|审核|核对|留意", request[0]):
            return (f"谢谢对方的提醒，问问核对「{detail}」时应以哪个版本为准。",
                    f"告诉对方已收到关于「{detail}」的核对要求。")
        if re.search(r"补充|提交|发送|发来|提供|准备", request[0]):
            return (f"告诉对方已收到关于「{detail}」的要求。",
                    f"询问「{detail}」有没有可参考的示例。")

    event = _EVENT.search(body) or _EVENT.search(subject)
    if event and not _MINUTES.search(context):
        return _event_suggestions(event[0], body, subject)
    if request:
        return (f"谢谢对方的提醒，告诉对方已收到关于「{_label(request[1])}」的请求。",)
    question = re.search(r"([^。！？?!\n]{2,100})[？?]", body)
    if question:
        return (f"告诉对方已看到「{_label(question[1])}？」这个问题，问问是否有补充背景。",)
    pr = re.match(r"\[[^\]]+/[^\]]+\]\s*(.+)", subject)
    if pr and re.search(r"pull request|\bPR\b|#\d+|review|comment", context, re.I):
        comment = re.search(r"\b(?:please|could you|can you)\s+([^.!?\n]{3,70})", body, re.I)
        if comment:
            return (f"谢谢对方的反馈，告诉对方已看到关于「{_label(comment[1])}」的建议。",)
        return (f"谢谢对方的反馈，告诉对方已看到关于「{_label(pr[1])}」的更新。",)

    # The client already supplies decoded plain text, including for HTML-only messages.
    title = re.search(r"《[^《》\n]{1,42}》", body)
    if title and "领取" in body:
        return (f"谢谢对方的提醒，问问几点方便领取{title[0]}。",)
    for line in re.split(r"[。！!\n]", body):
        line = re.sub(r"^(?:您好|你好|大家好|下午好|上午好)[，,：:\s]*", "", line.strip())
        if len(line) >= 5 and not re.search(r"https?://|^[-_]+$|没有可预览的文本正文", line):
            return (f"告诉对方已看到来信提到的「{_label(line)}」，谢谢对方的分享。",)
    if subject:
        return (f"告诉对方已收到关于「{_label(subject)}」的来信。",)
    return ()
