"""Resolve a date and time quoted from a message into an editable deadline."""

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december")
_MONTH_NUMBERS = {term: n for n, name in enumerate(_MONTHS, 1) for term in (name, name[:3])}
_WEEKDAY_NUMBERS = {term: n for n, name in enumerate(_WEEKDAYS) for term in (name, name[:3])}
_CN_DIGITS = {c: str(n) for n, c in enumerate("零一二三四五六七八九")} | {"〇": "0", "两": "2"}
_CN_NUMBER = re.compile("[零〇一二两三四五六七八九十]+")
_REGION_ZONES = {"ET": "America/New_York", "EASTERN TIME": "America/New_York",
                 "PT": "America/Los_Angeles", "PACIFIC TIME": "America/Los_Angeles"}
_OFFSETS = {"北京时间": 8, "中国标准时间": 8, "CHINA STANDARD TIME": 8, "日本时间": 9,
            "JAPAN STANDARD TIME": 9, "JST": 9, "HKT": 8, "UTC": 0, "GMT": 0, "Z": 0,
            "EDT": -4, "EST": -5, "PDT": -7, "PST": -8, "EASTERN STANDARD TIME": -5,
            "EASTERN DAYLIGHT TIME": -4, "PACIFIC STANDARD TIME": -8, "PACIFIC DAYLIGHT TIME": -7}

_DAYPART = r"(?:凌晨|早上|上午|中午|下午|晚上)"
_CN_HOUR = rf"(?:{_DAYPART}\s*)?[零〇一二两三四五六七八九十\d]{{1,3}}[点时]"
_CN_MINUTE = r"(?:半|一刻|三刻|[零〇一二两三四五六七八九十\d]{1,3}分?)?"
_MERIDIEM = r"(?:a\.?m\.?|p\.?m\.?)"
_CLOCK_IN_TEXT = re.compile(
    rf"{_CN_HOUR}{_CN_MINUTE}|(?<!\d)(?:{_DAYPART}\s*)?\d{{1,2}}[:：]\d{{2}}(?:\s*{_MERIDIEM})?"
    rf"|(?<!\d)\d{{1,2}}\s*{_MERIDIEM}(?![a-z])|\bnoon\b",
    re.I,
)
_ZONE_IN_TEXT = re.compile(
    r"(?:北京|中国标准|日本|新加坡|香港|美国东部|美国西部|英国|当地)时间|"
    r"(?<![A-Za-z])(?:UTC|GMT)(?:\s*[+-]\d{1,2}(?::?\d{2})?)?(?![A-Za-z])|"
    r"(?<![A-Za-z])(?:[EMPC][DS]T|[EMPC]T|IST|JST|CET|CEST|BST|HKT|SGT|AEST|AEDT)(?![A-Za-z])|"
    r"(?<![A-Za-z])(?:Eastern|Pacific|Central|Mountain|India|China|Japan|London)\s+(?:Standard\s+|Daylight\s+)?Time\b|"
    r"(?<![A-Za-z])(?:Asia|America|Europe|Africa|Australia|Pacific|Atlantic|Indian)/[A-Za-z_/+-]+",
    re.I,
)


def _arabic_numbers(text):
    def convert(match):
        value = match[0]
        if "十" not in value:
            return "".join(_CN_DIGITS[c] for c in value)
        tens, _, ones = value.partition("十")
        if len(tens) > 1 or len(ones) > 1 or ones == "十":
            raise ValueError("中文日期或时间格式无法识别，请核实原文。")
        return str(int(_CN_DIGITS.get(tens, "1")) * 10 + int(_CN_DIGITS.get(ones, "0")))
    return _CN_NUMBER.sub(convert, text)


def _zone(text):
    raw = text.strip().upper()
    if not raw:
        return None  # Local timezone rules must be applied on the deadline's date.
    if raw in _OFFSETS:
        return timezone(timedelta(hours=_OFFSETS[raw]))
    name = _REGION_ZONES.get(raw)
    if name is None and re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z_0-9+-]+)+", text):
        name = text
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            raise ValueError("本机没有该地区的时区数据，请核实原文中明确的 UTC 偏移量。") from None
    match = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", raw)
    if match:
        hours, minutes = int(match[2]), int(match[3] or 0)
        if hours <= 14 and minutes < 60 and (hours < 14 or not minutes):
            return timezone((1 if match[1] == "+" else -1) * timedelta(hours=hours, minutes=minutes))
    raise ValueError("原文时区无法唯一确定，请核实所在地区或 UTC 偏移量。")


def _relative_day(raw, sent_day):
    days = {"今天": 0, "今日": 0, "today": 0, "明天": 1, "明日": 1, "tomorrow": 1,
            "后天": 2, "the day after tomorrow": 2}
    if raw in days:
        return sent_day + timedelta(days=days[raw])
    match = re.fullmatch(r"(本|这|下)?(?:周|星期|礼拜)([一二三四五六日天])", raw)
    if match:
        weekday = min("一二三四五六日天".index(match[2]), 6)
        qualifier = match[1]
    else:
        match = re.fullmatch(r"(?:on )?(?:(this|next) )?([a-z]+)\.?", raw)
        if not match or match[2] not in _WEEKDAY_NUMBERS:
            raise ValueError("无法确定原文的日期，请核实具体的年、月、日。")
        weekday = _WEEKDAY_NUMBERS[match[2]]
        qualifier = match[1]
    delta = weekday - sent_day.weekday()
    if qualifier in ("下", "next"):
        delta += 7
    elif not qualifier:
        delta %= 7
    return sent_day + timedelta(days=delta)


def _calendar_day(raw, sent_day):
    raw = raw.strip().casefold()
    match = re.fullmatch(r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})[日号]?", _arabic_numbers(raw))
    if match:
        year, month, day = match.groups()
    else:
        english = " ".join(re.sub(r"(?<=\d)(st|nd|rd|th)\b|[,\.]", "", raw).split())
        match = re.fullmatch(r"([a-z]+) (\d{1,2})(?: (\d{4}))?", english)
        if match and match[1] in _MONTH_NUMBERS:
            month, day, year = _MONTH_NUMBERS[match[1]], match[2], match[3]
        else:
            match = re.fullmatch(r"(\d{1,2}) ([a-z]+)(?: (\d{4}))?", english)
            if match and match[2] in _MONTH_NUMBERS:
                day, month, year = match[1], _MONTH_NUMBERS[match[2]], match[3]
            elif sent_day:
                return _relative_day(raw, sent_day)
            else:
                raise ValueError("缺少来信日期，请核实完整的截止日期。")
    if not year:
        if not sent_day:
            raise ValueError("无法确定来信日期，不能推断年份。")
        year = sent_day.year + int(sent_day.month == 12 and int(month) == 1)
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        raise ValueError("具体日期不存在，请核实截止年月日。") from None


def _clock(raw):
    raw = raw.strip().casefold().replace("：", ":").replace(".", "").replace("一刻", "15分").replace("三刻", "45分")
    if not raw:
        return time(23, 59)  # The confirmation card labels this as a date-only suggestion.
    if raw == "noon":
        return time(12)
    m = re.fullmatch(r"(凌晨|早上|上午|中午|下午|晚上)?\s*(\d{1,2})(?::(\d{2})|[点时](?:(半)|(\d{1,2})分?)?)?\s*(am|pm)?",
                     _arabic_numbers(raw))
    if not m or (m[1] and m[6]):
        raise ValueError("无法确定原文时刻，请核实小时、分钟和上下午。")
    hour = int(m[2])
    half_day = m[1] or m[6]
    if half_day:
        if not 1 <= hour <= 12:
            raise ValueError("原文的小时和上下午不一致，请核实时刻。")
        if half_day == "中午":
            if hour not in (11, 12, 1):
                raise ValueError("无法确定原文时刻。")
            hour = 13 if hour == 1 else hour
        else:
            hour = hour % 12 + (12 if half_day in ("下午", "晚上", "pm") else 0)
    minutes = int(m[3] or m[5] or (30 if m[4] else 0))
    if hour > 23 or minutes > 59:
        raise ValueError("原文时刻超出有效范围，请核实小时和分钟。")
    return time(hour, minutes)


def _check_evidence(evidence, clock_text, zone_text):
    zone_matches = list(_ZONE_IN_TEXT.finditer(evidence))
    clocks = [m for m in _CLOCK_IN_TEXT.finditer(evidence)
              if not any(z.start() <= m.start() and m.end() <= z.end() for z in zone_matches)]
    if clocks and not clock_text:
        raise ValueError("引文含有明确时刻，请逐字提取完整的小时、分钟和上下午。")
    if not clock_text and re.search(r"下班[之以]?前|\b(?:EOD|close of business|midnight)\b", evidence, re.I):
        raise ValueError("原文没有给出可唯一确定的钟点，请核实当地具体截止时间。")
    if clocks and not any(_clock(match[0]) == _clock(clock_text) for match in clocks):
        raise ValueError("提取的时刻与原文不符，请核实完整时间。")
    zones = [match[0].strip().casefold() for match in zone_matches]
    for clock in clocks:
        # Numeric offsets and unrecognized timezone acronyms directly following the clock.
        suffix = evidence[clock.end():].lstrip(" (（\t")
        if _ZONE_IN_TEXT.match(suffix):
            continue
        extra = re.match(r"(Z|[+-]\d{2}:?\d{2}|[A-Z]{2,5})(?![a-zA-Z0-9])", suffix)
        if extra and extra[1] not in {"RSVP", "ASAP"}:
            zones.append(extra[1].casefold())
    if zones and (not zone_text or not any(zone_text.casefold() == zone for zone in zones)):
        raise ValueError("引文含有时区，请逐字提取完整时区并核实 UTC 偏移量。")


def _adjacent_context(source, quote):
    """Include only clock/zone tokens immediately next to the model's verbatim quote."""
    start = source.find(quote)
    if start == -1:
        return quote
    before = source[max(0, start - 48):start].rstrip(" (（,，\t")
    prefix = next((z[0] for z in _ZONE_IN_TEXT.finditer(before) if z.end() == len(before)), "")
    tail = source[start + len(quote):start + len(quote) + 80]
    consumed = 0
    while consumed < len(tail):
        connector = re.match(r"[ \t,，(（]*(?:at[ \t]+)?", tail[consumed:], re.I)
        remaining = tail[consumed + connector.end():]
        part = (_CLOCK_IN_TEXT.match(remaining) or _ZONE_IN_TEXT.match(remaining)
                or re.match(rf"{_MERIDIEM}(?![a-z])", remaining, re.I)
                or re.match(r"[+-]\d{2}:?\d{2}|[A-Z]{2,5}(?![A-Za-z])", remaining))
        if not part:
            break
        consumed += connector.end() + part.end()
    return prefix + " " + quote + tail[:consumed]


def resolve_deadline(date_text, time_text, zone_text, sent_timestamp, *, evidence="", source_text=""):
    if evidence:
        _check_evidence(_adjacent_context(source_text, evidence), time_text, zone_text)
    zone = _zone(zone_text)
    sent = datetime.fromtimestamp(sent_timestamp, zone).date() if sent_timestamp > 0 else None
    day = _calendar_day(date_text, sent)
    wall_clock = datetime.combine(day, _clock(time_text))
    # A skipped hour has no matching instant. A repeated hour has two, even for
    # a system-local time with no portable IANA name.
    candidates = {}
    for fold in (0, 1):
        candidate = wall_clock.replace(tzinfo=zone, fold=fold)
        timestamp = candidate.timestamp()
        if datetime.fromtimestamp(timestamp, zone).replace(tzinfo=None) == wall_clock:
            candidates.setdefault(timestamp, candidate if zone else candidate.astimezone())
    if len(candidates) != 1:
        raise ValueError("该时刻遇到夏令时切换，可能重复或不存在，请核实 UTC 偏移量。")
    return next(iter(candidates.values()))
