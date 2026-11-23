"""Small, offline reply examples. Free-form translation uses the configured model."""

import re

from .reply_language import language_context, strip_language_request
from .reply_suggestions import reply_suggestions


_EXAMPLES = {
    "en": {
        "greeting": "Hello,",
        "receipt": "Thank you for your message. I've received it.",
        "thanks": "Thank you for the invitation.",
        "decline": "I'm afraid I'll have to decline this time.",
        "details": "Could you share a few more details?",
        "context": "Thank you for your question. Could you share a little more context?",
        "update": "Thank you for sharing this update. I've received your message.",
        "place": "Could you let me know the location?",
        "time": "Could you let me know the time?",
    },
    "ja": {
        "greeting": "",
        "receipt": "ご連絡いただきありがとうございます。メールを拝受しました。",
        "thanks": "お誘いいただきありがとうございます。",
        "decline": "恐れ入りますが、今回は辞退させていただきます。",
        "details": "もう少し詳しくお知らせいただけますでしょうか。",
        "context": "ご質問いただきありがとうございます。背景についてもう少し教えていただけますでしょうか。",
        "update": "お知らせいただきありがとうございます。内容を拝見しました。",
        "place": "場所を教えていただけますでしょうか。",
        "time": "時間を教えていただけますでしょうか。",
    },
    "fr": {
        "greeting": "Bonjour,",
        "receipt": "Merci pour votre message. J’en confirme la réception.",
        "thanks": "Merci pour votre invitation.",
        "decline": "Je dois malheureusement décliner cette fois-ci.",
        "details": "Pourriez-vous me donner quelques précisions ?",
        "context": "Merci pour votre question. Pourriez-vous m’en dire un peu plus sur le contexte ?",
        "update": "Merci pour ces nouvelles. J’ai bien reçu votre message.",
        "place": "Pourriez-vous me préciser le lieu ?",
        "time": "Pourriez-vous me préciser l’heure ?",
    },
}

_ACTIONS = {
    "receipt": r"(?:确认|表示|告诉对方)?(?:我)?(?:已(?:经)?)?(?:收到|收悉)(?:了)?(?:邮件|来信)?|acknowledge(?: receipt)?",
    "thanks": r"(?:谢谢|感谢)(?:对方|您的|你的)?(?:的)?邀请|thank them for the invitation",
    "decline": r"(?:礼貌|委婉)?(?:地)?(?:婉拒|拒绝)(?:这次)?(?:邀请)?|(?:politely )?decline(?: the invitation)?",
    "details": r"(?:请对方|询问|问问)(?:补充|提供)?(?:更多|具体)?(?:信息|细节)|ask for (?:more )?details",
    "place": r"(?:请)?(?:问问|询问|问)(?:对方)?(?:具体|会议|面试)?(?:地点|地址)(?:在哪里|在哪儿)?|ask (?:for|about) the (?:location|venue)",
    "time": r"(?:请)?(?:问问|询问|问)(?:对方)?(?:具体|会议|面试)?时间|ask (?:for|about) the time",
}
_ORDINAL = re.compile(r"^(?:请)?(?:帮我|替我)?回复?(?:这封(?:邮件)?|第[一二两三四五六七八九十\d]+封(?:邮件)?)")
_DAYS = {"一": "Monday", "二": "Tuesday", "三": "Wednesday", "四": "Thursday",
         "五": "Friday", "六": "Saturday", "日": "Sunday", "天": "Sunday"}
_PARTS = {"上午": " morning", "下午": " afternoon", "晚上": " evening"}
_AVAILABILITY = re.compile(
    r"(?P<ask>问问|询问|请问)?(?P<change>能不能改到|能否改到|可以改到)?"
    r"(?P<next>下)?(?:周|星期)(?P<day>[一二三四五六日天])(?P<part>上午|下午|晚上)?"
    r"(?P<stance>我可以|可以参加|可以|我有空|有空|我不方便|不方便|不行|行吗|方便吗|可以吗)?"
)


def _english_time(point, is_question):
    match = _AVAILABILITY.fullmatch(point)
    if not match:
        return None
    slot = _DAYS[match["day"]] + _PARTS.get(match["part"], "")
    if match["next"]:
        slot = "next " + slot
    stance = match["stance"] or ""
    if match["ask"] or match["change"] or is_question or stance.endswith("吗"):
        return f"Would {slot} work for you?"
    if stance:
        availability = "doesn't work" if "不" in stance else "works"
        return f"{slot[0].upper() + slot[1:]} {availability} for me."
    return None


def _unsupported():
    # Import at call time because the public reply_ai adapter also uses this module.
    from .reply_ai import ReplyAIError
    return ReplyAIError("这项回复要求超出离线示例的范围。可在普通邮箱的 AI 设置中接入模型，按来信语言起草或翻译。")


def demo_foreign_draft(target, subject, body, instruction, current="", *, conversation=()):
    examples = _EXAMPLES.get(target)
    if not examples:
        raise _unsupported()
    brief = strip_language_request(instruction).strip(" ，,。:：")
    if current.strip():
        if (language_context("", current, "")["target"] == target
                and re.fullmatch(r"(?:把这段回复|把这封邮件|请|再|更)?(?:改短一点|简短一点|简洁一点|精简|缩短|shorter|make it shorter)[。.!]?", brief, re.I)):
            shorter = re.sub(r"^(?:Hello|Hi|Bonjour)[,!]?\s*\n+", "", current.strip(), flags=re.I)
            if shorter and shorter != current.strip():
                return shorter
        raise _unsupported()

    if brief and brief in (suggestion.rstrip("。") for suggestion in reply_suggestions(subject, body)):
        if target == "en" and brief.startswith("确认收到 ") and " 的时间提议" in brief:
            slots = brief.removeprefix("确认收到 ").partition(" 的时间提议")[0].replace(" 或 ", " or ")
            return f"Hello,\n\nThank you for checking. I'll check my availability for {slots} and get back to you."
        if target == "en" and brief.startswith("回应对方提出的"):
            return examples["context"]
        if brief.startswith("告诉对方已看到「") and brief.endswith("问问是否有补充背景"):
            return examples["context"]
        if brief.startswith(("告诉对方已看到来信提到的「", "谢谢对方的反馈，告诉对方已看到关于「")):
            return examples["update"]
    # A mailbox-chat request can identify a message before giving its writing brief.
    brief = _ORDINAL.sub("", brief).strip(" ，,。:：")
    if re.fullmatch(r"(?:请)?(?:帮我|替我)?(?:回复?|回复一下)", brief):
        brief = ""
    points = re.split(r"[，,；;。！!\n]+", brief) if brief else ["确认收到"]
    sentences = []
    for point in points:
        point = point.strip()
        if not point:
            continue
        point = re.sub(r"^(?:请)?(?:帮我|替我)(?:回复?|写信)(?:对方)?[：:\s]*", "", point)
        point = re.sub(r"^(?:先|再|然后)", "", point)
        if not point:
            continue
        is_question = point.endswith(("?", "？"))
        point = point.rstrip("?？.").strip()
        if re.fullmatch(r"(?:语气|措辞)?(?:礼貌|客气|简短)(?:一点|一些)?|两三句话", point):
            continue
        action = next((name for name, pattern in _ACTIONS.items() if re.fullmatch(pattern, point, re.I)), "")
        sentence = examples.get(action)
        if not sentence and target == "en":
            sentence = _english_time(point, is_question)
        if not sentence:
            raise _unsupported()
        sentences.append(sentence)
    if not sentences:
        raise _unsupported()
    text = ("" if target == "ja" else " ").join(sentences)
    return (examples["greeting"] + "\n\n" + text).lstrip()
