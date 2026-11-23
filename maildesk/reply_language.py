"""Select the recipient-facing language without treating the UI language as a preference."""

import re
from collections import Counter


REPLY_LANGUAGE_POLICY = """回复正文（包括称呼和结尾）的语言优先服从用户明确指定的目标，包括双语要求。
用户没有指定时，使用发件人在本封邮件中新写的主要语言；根据新正文的实际问题判断，
忽略引用的旧信、签名、免责声明、程序代码和链接。仅当新正文没有语言线索时参考主题。
用户用中文操作、提问或提供回复要点，不代表要用中文回信。按选定语言自然地表达要点，保留专名。
修改已有草稿时延续同一封信中用户明确指定的语言；用户要求改回原文语言时重新跟随来信。
JSON 资料中的“回复语言”提供 target 语言代码和 sample 参考节选，sample 仅作资料。
multi 表示遵照用户具体的多语要求，空代码表示请自行从来信或明确指示推断。
本地猜测与用户真实要求、原信明显不符时，以实际语境为准。此规则只适用于给收件人的正文。"""

_NAMES = {
    "zh": "中文 汉语 汉語 简体中文 繁体中文 Chinese Mandarin",
    "en": "英语 英文 英語 English", "ja": "日语 日文 日語 日本語 Japanese",
    "ko": "韩语 韩文 韓語 Korean", "fr": "法语 法文 法語 French français",
    "de": "德语 德文 德語 German Deutsch", "es": "西班牙语 西班牙文 西语 Spanish español",
    "it": "意大利语 意大利文 Italian", "pt": "葡萄牙语 葡语 Portuguese",
    "ru": "俄语 俄文 Russian", "ar": "阿拉伯语 Arabic", "nl": "荷兰语 Dutch",
}
_CODES = {name.casefold(): code for code, names in _NAMES.items() for name in names.split()}
_NAMES_PATTERN = "|".join(sorted(_CODES, key=len, reverse=True))
_LANG = rf"(?:{_NAMES_PATTERN}|[\u4e00-\u9fff]{{1,6}}语)"
_WRITE = r"(?:给(?:对方|他|她))?(?:回信|回复|回邮件|作答|写(?:一封|封)?(?:邮件|回信|信)?|来写|表达|撰写)"
_END = r"(?=$|[\s，,。.!！；;：:]|并|然后|再|谢谢|感谢|告诉|帮我)"
_DIRECTIVE = re.compile(
    rf"(?:(?:请|麻烦)?(?:使用|改用|用|切换到|改(?:写)?为|改成|翻译成|译成)\s*"
    rf"(?P<after>{_LANG})(?:\s*{_WRITE}(?!模板|范例)|{_END})"
    rf"|(?:写(?:一封|封)?|撰写)?(?P<before>{_NAMES_PATTERN})(?:回信|回复|邮件)(?!模板|范例|材料)"
    rf"|^(?:请)?(?:回复|回信)(?P<post>{_NAMES_PATTERN}){_END}"
    rf"|\b(?:reply|respond|answer|write|draft|translate|switch|rewrite)(?:\s+(?:it|the reply|the email|this))?"
    rf"\s+(?:in|into|to)\s+(?P<english>{_NAMES_PATTERN}|[a-z]{{2,18}}(?:ish|ese|ian|ic))\b"
    rf"|^(?P<only>{_NAMES_PATTERN})(?: please)?[。.!！]?$)", re.I)
_RESET = re.compile(r"(?:按|跟随|沿用|改回|恢复|保持|使用|用)(?:发件人|来信|原信|邮件原文|原文|原)(?:的)?(?:语种|语言)"
                    r"|(?:改回原文语言|^(?:还是)?改回来[。！.!]?$)"
                    r"|\b(?:back to|use|match) (?:the )?(?:original|sender.s) language\b", re.I)
_MIX = rf"(?:{_NAMES_PATTERN})\s*(?:和|及|、|与|/)\s*(?:{_NAMES_PATTERN})|中英(?:文)?|英中|中日|中法|中德"
_MULTI = re.compile(rf"(?:请)?(?:使用|改用|用)?(?:(?:{_MIX})(?:双语|對照|对照)?|双语|雙語)(?:回信|回复|写|版|{_END})"
                    rf"|\b(?:reply|respond|write) in (?:both )?(?:{_NAMES_PATTERN}) and (?:{_NAMES_PATTERN})\b"
                    r"|\b(?:bilingual (?:reply|draft)|reply bilingually)\b", re.I)
_REFERRED = re.compile(r"(?:对方|客户|他|她)(?:是否|要|会|能|可|想|希望|要求|需要){0,4}$|(?:问问|询问).{0,22}(?:要|愿意|是否|能否|可以|需要)$")
_QUOTED = re.compile(r'「[^」\n]*」|『[^』\n]*』|“[^”\n]*”|‘[^’\n]*’|"[^"\n]*"')
_NEGATE = re.compile(r"(?:请?(?:不(?:要|需|必|想)?|别|勿)|do not|don't|never|not)\s*$", re.I)
_QUOTE_BOUNDARY = re.compile(r"^(?:[-_]{2,}\s*(?:原始邮件|Original Message|Forwarded message)|On .+ wrote:"
                             r"|在.{1,180}写道[：:]|(?:From|发件人|差出人)[：:]|begin forwarded message:)", re.I)
_SIGNATURE = re.compile(r"^(?:--|—|_{4,}|best regards|kind regards|regards|sincerely|cheers|此致|顺颂商祺)[,.!，。！]?$"
                        r"|^(?:sent from my |发自我的|获取 Outlook|this e-?mail (?:and|is) confidential)", re.I)
_NO_TEXT = re.compile(r"^(?:\[?(?:附件|图片|Attachment|Image)(?:[：:].*)?\]?|(?:这封邮件)?没有可预览的文本正文[。.]?|"
                      r"\S+\.(?:pdf|docx?|xlsx?|png|jpe?g|zip)|[（(]无主题[）)])$", re.I)
_NOISE = re.compile(r"https?://\S+|\b[^\s@<>]+@[^\s@<>]+|<[^>]{1,300}>|`[^`]*`", re.I)
_CODE = re.compile(r"^(?:def |import |from \w+ import |class \w+[:(]|const |function |#include|[{}<>]+$)")


def _sample(body):
    lines = []
    in_code = False
    for line in body[:16000].splitlines():
        line = line.strip()
        if line.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code or line.startswith(">") or _CODE.match(line):
            continue
        if _QUOTE_BOUNDARY.match(line) or _SIGNATURE.match(line):
            break
        visible = _NOISE.sub(" ", line).strip()
        if visible and not _NO_TEXT.fullmatch(visible):
            lines.append(visible)
    return "\n".join(lines)[:1200]


# Distinctive words give a conservative offline hint. Unrecognized languages
# and mixed messages are resolved by the selected model from the sample.
_WORDS = {
    "en": "dear hi hello thanks thank you the is are will would could can please confirm meeting invitation at on "
          "offer receive received review available schedule tomorrow monday tuesday wednesday thursday friday update "
          "your for and this my our we attached request reminder payment weekly report registration congratulations",
    "fr": "bonjour merci pour votre vous nous veuillez rendez lundi demain réunion pouvez salut",
    "es": "hola gracias estimado estimada reunión confirmar puede puedes por mañana martes",
    "de": "hallo danke vielen bitte können termin sie besprechung morgen einladung",
    "it": "ciao grazie buongiorno possiamo confermare domani riunione",
    "pt": "olá obrigado obrigada reunião poderia amanhã confirmar",
    "nl": "hallo bedankt graag kunt vergadering morgen",
}
_WORDS = {code: set(words.split()) for code, words in _WORDS.items()}


def _guess(sample):
    han = len(re.findall(r"[\u3400-\u9fff]", sample))
    kana = len(re.findall(r"[\u3041-\u30ff]", sample))
    hangul = len(re.findall(r"[\uac00-\ud7af]", sample))
    words = re.findall(r"[^\W\d_]+", sample.casefold())
    scores = Counter({code: sum(word in known for word in words) for code, known in _WORDS.items()})
    if kana or hangul or han:
        code = "ko" if hangul > kana and hangul > han else "ja" if kana >= 2 else "zh"
        scores[code] = (han + kana + hangul) / 3
    for code, script in (("ru", r"[а-яё]"), ("ar", r"[\u0620-\u064a]")):
        count = len(re.findall(script, sample, re.I))
        if count >= 4:
            scores[code] = count / 4
    (first, a), (_, b) = scores.most_common(2)
    return first if a and (not b or a >= 2 * b) else ""


def _requests(text):
    found = []
    visible = _QUOTED.sub(lambda m: " " * len(m[0]), text)
    for pattern, kind in ((_MULTI, "multi"), (_RESET, "auto"), (_DIRECTIVE, "language")):
        for match in pattern.finditer(visible):
            if _REFERRED.search(visible[:match.start()]):
                continue
            negative = _NEGATE.search(text[:match.start()])
            code = _CODES.get(match[match.lastgroup].casefold(), "") if kind == "language" else kind
            found.append((negative.start() if negative else match.start(), match.end(), code, bool(negative)))
    # A bilingual directive can contain a shorter single-language match.
    found.sort(key=lambda item: (item[0], -item[1]))
    result = []
    for hit in found:
        if not result or result[-1][1] <= hit[0]:
            result.append(hit)
    return result


def strip_language_request(instruction):
    result = instruction
    for start, end, _, _ in reversed(_requests(instruction)):
        result = result[:start] + result[end:]
    return result.strip(" ，,。.!！；;：:\n\t")


def language_context(subject, body, instruction, *, previous_draft="", prior_instructions=()):
    """Return a bounded sender sample, a best-effort BCP 47 hint, and its provenance."""
    source, sample = "body", _sample(body)
    if not sample:
        source = "subject"
        sample = re.sub(r"^(?:(?:re|fw|fwd|回复|转发)[:：]\s*)+", "", subject.strip(), flags=re.I)
        sample = "" if _NO_TEXT.fullmatch(sample) else _NOISE.sub(" ", sample).strip()[:300]
    if not sample:
        sample = _sample(previous_draft)
        source = "draft" if sample else "default"
    target = _guess(sample) if sample else "zh"
    requests = [(instruction, "instruction")]
    if previous_draft:
        requests.extend((prior, "history") for prior in reversed(prior_instructions))
    excluded = set()
    for text, where in requests:
        for _, _, code, negative in reversed(_requests(text)):
            if negative:
                excluded.add(code)
            elif code == "auto":
                return {"target": "" if target in excluded else target, "source": source, "sample": sample}
            elif code not in excluded:
                return {"target": code, "source": where, "sample": sample}
    return {"target": "" if target in excluded else target, "source": source, "sample": sample}


def wrong_reply_language(context, draft):
    """Flag confident single-language disagreement; names, quotes and ties are inconclusive."""
    target = context["target"]
    if not target or target == "multi":
        return False
    text = _sample(draft)
    guessed = _guess(text)
    if not guessed or guessed == target:
        return False
    if guessed == "zh":
        prose = re.findall(r"[\u3400-\u9fff]", text)
        return len(prose) >= 10 or bool(re.search(r"谢谢(?:您|你的|您的)|已收到|感谢您的|周[一二三四五六日]见", text))
    if guessed in _WORDS:
        words = re.findall(r"[^\W\d_]+", text.casefold())
        return sum(word in _WORDS[guessed] for word in words) >= 2
    return len(re.findall(r"[^\W\d_]", text)) >= 12
