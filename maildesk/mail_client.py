"""IMAP inbox access and SMTP replies for supported mail providers."""

import base64
import imaplib
import re
import smtplib
import ssl
from collections import OrderedDict
from contextlib import closing, contextmanager, suppress
from dataclasses import replace
from datetime import timezone
from email import policy
from email.errors import CloseBoundaryNotFoundDefect, MessageError
from email.header import decode_header
from email.parser import BytesFeedParser, BytesHeaderParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from threading import Lock
from urllib.parse import urljoin

from .mail_replies import reply_envelope, reply_message
from .models import (
    PAGE_SIZE, Account, ArrivalBatch, InboxCursor, MailBody, MailItem, MailPage, MailQuery, MailSearchResult,
    ReplyEnvelope, normalize_cid,
)
from .providers import MAIL_PROVIDERS


_TIMEOUT = 12
_BODY_LIMIT = 2 * 1024 * 1024
_RICH_BODY_LIMIT = 8 * 1024 * 1024
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
_HEADER_FIELDS = "(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT)]<0.16384>)"
_PREVIEW_OCTETS = 12288
_DATE_BATCH = 256
_DATE_CACHE_LIMIT = 16_384
_SEARCH_DATE_CAP = 2048
_SEARCH_PREVIEW_CAP = 80
_UID = re.compile(rb"\bUID\s+(\d+)\b", re.I)
_INTERNAL_DATE = re.compile(rb'\bINTERNALDATE\s+"([^"]+)"', re.I)
_FLAGS = re.compile(rb"\bFLAGS\s*\(([^)]*)\)", re.I)
_QUOTED_PREVIEW = re.compile(rb'\bPREVIEW\s+"((?:[^"\\]|\\.)*)"', re.I)
_LOGIN_REJECTION = re.compile(
    r"^(\[AUTHENTICATIONFAILED\]|(?:login|authentication) failed|invalid (?:credentials|username|password))",
    re.IGNORECASE,
)


class MailClientError(RuntimeError):
    """A message suitable for display in the desktop client."""


class ReplyDeliveryUncertain(MailClientError):
    """The connection ended before SMTP confirmed or rejected the message."""


def _ok(response):
    kind, data = response
    if kind != "OK":
        raise MailClientError("邮件服务器未完成此次请求，请刷新后重试。")
    return data


def _has_capability(client, capability):
    # imaplib decodes CAPABILITY tokens to str; adapters may expose raw IMAP bytes.
    for value in client.capabilities:
        name = value.decode("ascii") if isinstance(value, bytes) else value
        if name.partition("=")[0].upper() == capability:
            return True
    return False


def _decode(raw, charset=None, *, partial=False):
    # GB18030 also covers GBK/GB2312 text with an omitted or incomplete label.
    for encoding in (charset, "utf-8", "gb18030"):
        if encoding:
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError as exc:
                if partial and exc.end == len(raw):
                    return raw[:exc.start].decode(encoding)
            except (LookupError, UnicodeError):
                continue
    return raw.decode("utf-8", errors="replace")


def _header(value):
    if value is None:
        return ""
    try:
        parts = decode_header(value)
    except (ValueError, LookupError):
        parts = [(str(value), None)]
    text = "".join(_decode(chunk, charset) if isinstance(chunk, bytes) else chunk for chunk, charset in parts)
    return " ".join(text.split())


class _HTMLText(HTMLParser):
    _hidden = {"head", "script", "style", "template", "title"}
    _blocks = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "section", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.depth = 0
        self.images = set()
        self.base = ""

    def handle_starttag(self, tag, attrs):
        if tag == "base" and not self.base:
            self.base = dict(attrs).get("href") or ""
        if tag in {"img", "html:img", "xhtml:img"} and not self.depth:
            fields = dict(attrs)
            src = fields.get("src") or fields.get("data-src")
            if src:
                self.images.add(src.strip())
        if tag in self._hidden:
            self.depth += 1
        elif tag in self._blocks and not self.depth:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in self._hidden and self.depth:
            self.depth -= 1
        elif tag in self._blocks and not self.depth:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.depth:
            self.text.append(data)


def _decode_text(body, partial):
    if partial and (body.get("Content-Transfer-Encoding") or "").lower() == "base64":
        # A bounded FETCH may stop in the middle of a base64 quartet.
        encoded = "".join(body.get_payload().split())
        try:
            payload = base64.b64decode(encoded[:len(encoded) // 4 * 4], validate=True)
        except ValueError:
            return ""
    else:
        payload = body.get_payload(decode=True) or b""
    return _decode(payload, body.get_content_charset(), partial=partial).strip()


def _parse_html(content):
    parser = _HTMLText()
    try:
        parser.feed(content)
        parser.close()
    except (AssertionError, ValueError):
        pass  # HTMLParser can reject malformed declarations after readable text.
    return parser


def _image_aliases(part):
    aliases = []
    if cid := part.get("Content-ID"):
        aliases.append("cid:" + str(cid).strip().strip("<>"))
    if location := part.get("Content-Location"):
        aliases.append(str(location).strip())
    return aliases


def _resource_leaves(node, related=None, inherited_base=""):
    # Nested messages and attached multipart documents have their own CID namespace.
    base = _document_base(node, inherited_base)
    if node.get_content_maintype() != "multipart":
        yield node, related, base
    elif not node.is_attachment():
        if node.get_content_subtype() == "related":
            related = node
        for child in node.iter_parts():
            yield from _resource_leaves(child, related, base)


def _unfinished_part(msg):
    # Only the final leaf of an unclosed multipart may end mid-image.
    while msg.get_content_maintype() == "multipart":
        if not any(isinstance(defect, CloseBoundaryNotFoundDefect) for defect in msg.defects):
            return None
        children = msg.get_payload()
        if not isinstance(children, list) or not children:
            return None
        msg = children[-1]
    return msg


def _resolve(base, location):
    try:
        return urljoin(base, location)
    except ValueError:
        return location


def _image_sources(sources, base):
    keys = {}
    for src in sources:
        for key in (normalize_cid(src), _resolve(base, src)):
            keys.setdefault(key, set()).add(src)
    return keys


def _document_base(part, inherited=""):
    base = _resolve(inherited, str(part.get("Content-Base", "")).strip())
    return _resolve(base, str(part.get("Content-Location", "")).strip())


def _matching_sources(part, keys, base, part_base):
    matches = set()
    for alias in _image_aliases(part):
        matches.update(keys.get(normalize_cid(alias), ()))
        matches.update(keys.get(_resolve(base, alias), ()))
    if part.get("Content-Location"):
        matches.update(keys.get(part_base, ()))
    return matches


def _image_body(msg, incomplete):
    images = {}
    for part, _, _ in _resource_leaves(msg):
        if (part is incomplete or part.get_content_type() not in _IMAGE_TYPES or part.is_attachment()
                or (part is not msg and part.get_content_disposition() != "inline")):
            continue
        resource = part.get_payload(decode=True)
        if resource and not part.defects:
            images[f"cid:maildesk-inline-{len(images)}"] = resource
    html = "".join(f'<p><img src="{src}" alt="邮件图片"></p>' for src in images)
    return MailBody("（图片邮件）" if images else "", html, images)


def _parse_mime(*segments):
    # Feed the original IMAP literals in bounded chunks. In particular, keep
    # MIME section headers and body continuations separate until they are parsed.
    parser = BytesFeedParser(policy=policy.default)
    for segment in segments:
        for start in range(0, len(segment), 64 * 1024):
            parser.feed(segment[start:start + 64 * 1024])
    return parser.close()


def _parse_body(raw, *continuations, rich=False, partial=False):
    msg = _parse_mime(raw, *continuations)
    body = _mime_body(msg, rich, partial)
    if rich and (not partial or re.search(rb"\r?\n\r?\n", raw)):
        return replace(body, reply=reply_envelope(msg))
    return body


def _mime_body(msg, rich, partial):
    for part in msg.walk():
        if part.get_content_maintype() == "text" and part.get_filename() and not part.is_attachment():
            # Older exporters use a file name alone for an ordinary text attachment.
            if part.get_content_disposition() != "inline":
                del part["Content-Disposition"]
                part["Content-Disposition"] = "attachment"
    plain_part = msg.get_body(preferencelist=("plain",))
    plain = _decode_text(plain_part, partial) if plain_part is not None else ""
    if plain and not rich:
        return MailBody(plain)
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is None:
        if rich and not plain:
            return _image_body(msg, _unfinished_part(msg) if partial else None)
        return MailBody(plain)
    html = _decode_text(html_part, partial)
    parsed = _parse_html(html)
    if not plain:
        plain = "\n".join(row.strip() for row in "".join(parsed.text).splitlines() if row.strip())
        if rich and not plain and parsed.images:
            plain = "（图片邮件）"
    if not rich:
        return MailBody(plain)

    leaves = list(_resource_leaves(msg))
    related, document_base = next(((group, base) for leaf, group, base in leaves if leaf is html_part), (None, ""))
    if not parsed.images:
        return MailBody(plain, html, base_url=document_base)
    incomplete = _unfinished_part(msg) if partial else None
    base = _resolve(document_base, parsed.base)
    sources = _image_sources(parsed.images, base)
    images = {}
    for part, group, part_base in leaves:
        if group is not related or part is incomplete or part.get_content_type() not in _IMAGE_TYPES:
            continue
        aliases = _matching_sources(part, sources, base, part_base)
        if aliases:
            image = part.get_payload(decode=True)
            if image and not part.defects:
                images.update((alias, image) for alias in aliases)
    return MailBody(plain, html, images, document_base)


def _body_text(raw, *, partial=False):
    return _parse_body(raw, partial=partial).text


def _fetch_literals(data):
    # A server may append UID or INTERNALDATE after the literal rather than before it.
    prefix, payload = b"", None
    for part in data:
        if isinstance(part, tuple):
            if payload is not None:
                yield prefix, payload
            prefix, payload = part
        elif payload is not None and isinstance(part, bytes):
            # A non-literal unsolicited FETCH (e.g. a FLAGS update) belongs
            # to its own message, not to the preceding literal.
            if re.match(rb"\d+\s+\(", part):
                yield prefix, payload
                prefix, payload = b"", None
            else:
                prefix += b" " + part
    if payload is not None:
        yield prefix, payload


def _server_previews(data):
    previews = {}
    for metadata, literal in _fetch_literals(data):
        uid = _UID.search(metadata)
        if uid and re.search(rb"\bPREVIEW\s+\{", metadata, re.I):
            previews[int(uid[1])] = literal.decode("utf-8", errors="replace")
    for line in data:
        if isinstance(line, bytes):
            match = _QUOTED_PREVIEW.search(line)
            if match:
                uid = _UID.search(line[:match.start()] + line[match.end():])
                if uid:
                    value = re.sub(rb'\\(["\\])', rb'\1', match[1])
                    previews[int(uid[1])] = value.decode("utf-8", errors="replace")
    return previews


def _list_previews(client, uids, *, required=False):
    if not uids:
        return {}
    uid_set = ",".join(str(uid) for uid in uids)
    if _has_capability(client, "PREVIEW"):
        try:
            kind, data = client.uid("FETCH", uid_set, "(UID PREVIEW)")
            if kind == "OK":
                previews = _server_previews(data)
                if not required or previews.keys() == set(uids):
                    return previews
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error:
            pass

    # Keep the headers and previews in separate batches: at most one literal
    # per FETCH record, regardless of which attribute the server sends first.
    previews = {}
    data = _ok(client.uid("FETCH", uid_set, f"(UID BODY.PEEK[]<0.{_PREVIEW_OCTETS}>)"))
    for metadata, raw in _fetch_literals(data):
        uid = _UID.search(metadata)
        if not uid:
            continue
        previews[int(uid[1])] = ""
        if re.search(rb"\r?\n\r?\n", raw):
            with suppress(ValueError, TypeError, RecursionError, MessageError):
                previews[int(uid[1])] = _body_text(raw, partial=True)
    if required and previews.keys() != set(uids):
        raise MailClientError("本次检查未取得完整的邮件摘要，请稍后重试。")
    return previews


def _fetch_part(client, uid, section, limit, *, offset=0):
    try:
        kind, data = client.uid("FETCH", str(uid), f"(UID BODY.PEEK[{section}]<{offset}.{limit}>)")
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        return None  # Some servers report BAD for a MIME section that does not exist.
    if kind == "OK":
        for metadata, raw in _fetch_literals(data):
            matched = _UID.search(metadata)
            if matched and int(matched[1]) == uid:
                if offset and f"BODY[{section}]<{offset}>".encode("ascii") not in metadata.upper():
                    continue
                return raw
    return None


def _mime_sections(client, uid, base):
    """Inspect only MIME headers, even when a large attachment precedes the body."""
    pending = [((1,), base)]
    # Bound round trips even for a message containing hundreds of attachments.
    for _ in range(24):
        if not pending:
            break
        path, parent_base = pending.pop()
        section = ".".join(map(str, path))
        headers = _fetch_part(client, uid, section + ".MIME", 8192)
        if not headers:
            continue  # End of this level in the MIME tree.
        pending.append((path[:-1] + (path[-1] + 1,), parent_base))
        part = BytesHeaderParser(policy=policy.default).parsebytes(headers)
        part_base = _document_base(part, parent_base)
        if part.get_content_maintype() == "multipart":
            if not part.is_attachment() and not part.get_filename():
                pending.append((path + (1,), part_base))
        else:
            yield section, part, headers.rstrip(b"\r\n") + b"\r\n\r\n", part_base


def _find_late_body(client, uid, initial, rich, root_base):
    body = initial
    images = []
    base = initial.base_url
    for section, part, headers, part_base in _mime_sections(client, uid, root_base):
        kind = part.get_content_type()
        if rich and kind in _IMAGE_TYPES and _image_aliases(part):
            images.append((section, part, headers, part_base))
            continue
        if part.is_attachment() or (part.get_filename() and part.get_content_disposition() != "inline"):
            continue
        if kind == "text/html" and not base:
            base = part_base
        if kind == "text/plain" and (body.text or body.html):
            continue
        if kind == "text/html" and (body.html or (body.text and not rich)):
            continue
        if kind not in ("text/plain", "text/html"):
            continue
        raw = _fetch_part(client, uid, section, _BODY_LIMIT)
        if raw:
            candidate = _parse_body(headers, raw, rich=rich, partial=len(raw) >= _BODY_LIMIT)
            if not rich and candidate.text:
                if len(raw) >= _BODY_LIMIT:
                    candidate = replace(candidate, text=candidate.text + "\n\n（正文较长，仅显示前 2 MiB 内的内容。）")
                return candidate
            body = replace(body, text=body.text or candidate.text, html=body.html or candidate.html,
                           base_url=body.base_url or (part_base if candidate.html else ""))
    if not rich or not body.html or not images:
        return body
    parsed = _parse_html(body.html)
    base = _resolve(base, parsed.base)
    sources = _image_sources(parsed.images - body.inline_images.keys(), base)
    resources = dict(body.inline_images)
    remaining = _RICH_BODY_LIMIT
    for section, part, headers, part_base in images:
        aliases = _matching_sources(part, sources, base, part_base) - resources.keys()
        if not aliases or remaining <= 0:
            continue
        raw = _fetch_part(client, uid, section, remaining + 1)
        if not raw:
            continue
        remaining -= len(raw)
        if remaining < 0:
            break
        image = _parse_mime(headers, raw)
        payload = image.get_payload(decode=True)
        if payload and not image.defects:
            resources.update((key, payload) for key in aliases)
    return replace(body, inline_images=resources)


def _mail_date(internal, header):
    for value in (internal, header):
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.astimezone().strftime("%Y-%m-%d %H:%M"), when.timestamp()
        except (TypeError, ValueError, OverflowError, OSError, IndexError):
            continue
    return header or "", 0.0


def _sequence_uid(client, sequence):
    data = _ok(client.fetch(str(sequence), "(UID)"))
    for line in data:
        if isinstance(line, bytes) and line.split(maxsplit=1)[:1] == [str(sequence).encode()]:
            match = _UID.search(line)
            if match:
                return int(match[1])
    raise MailClientError("收件箱正在变化，请刷新后重试。")


def _last_sequence_before(client, count, before_uid):
    """Binary-search ordered UIDs, including when the page anchor has been deleted."""
    low, high = 0, count + 1
    while high - low > 1:
        mid = (low + high) // 2
        if _sequence_uid(client, mid) < before_uid:
            low = mid
        else:
            high = mid
    return low


def _mail_headers(account, validity, data):
    items = []
    for metadata, raw in _fetch_literals(data):
        matched = _UID.search(metadata)
        if matched is None:
            raise MailClientError("邮件服务器返回的邮件标识不完整，请刷新后重试。")
        headers = BytesHeaderParser(policy=policy.compat32).parsebytes(raw)
        internal = _INTERNAL_DATE.search(metadata)
        flags_match = _FLAGS.search(metadata)
        flags = flags_match[1].lower().split() if flags_match else None
        date, timestamp = _mail_date(
            internal[1].decode("ascii", "replace") if internal else "", _header(headers["Date"])
        )
        items.append(MailItem(
            account=account.address, validity=validity, uid=int(matched[1]),
            subject=_header(headers["Subject"]) or "（无主题）", sender=_header(headers["From"]),
            recipient=_header(headers["To"]), date=date, timestamp=timestamp,
            unread=b"\\seen" not in flags if flags is not None else None,
            flagged=b"\\flagged" in flags if flags is not None else None,
        ))
    return items


def _with_previews(client, items, *, required=False):
    try:
        previews = _list_previews(client, [item.uid for item in items], required=required)
    except (imaplib.IMAP4.error, OSError, EOFError, MailClientError):
        if required:
            raise
        return items
    return [replace(item, preview=" ".join(previews.get(item.uid, "").split())[:200]) for item in items]


def _server_inbox_order(client, count):
    if not _has_capability(client, "SORT"):
        return None
    try:
        # RFC 5256 breaks ties by ascending sequence number. Reversing the whole
        # ascending result makes both INTERNALDATE and its UID tie-break descending.
        status, data = client.uid("SORT", "(ARRIVAL)", "US-ASCII", "ALL")
        if status == "OK":
            uids = _uid_list(data)
            if len(uids) == count and len(set(uids)) == count:
                return tuple(reversed(uids))
    except imaplib.IMAP4.abort:
        raise
    except (imaplib.IMAP4.error, MailClientError):
        pass
    return None


def _fetch_dates(client, uids):
    dates = {}
    for offset in range(0, len(uids), _DATE_BATCH):
        batch = uids[offset:offset + _DATE_BATCH]
        uid_set = ",".join(map(str, batch))
        records = _ok(client.uid("FETCH", uid_set, "(UID INTERNALDATE)"))
        requested = set(batch)
        for record in records:
            if not isinstance(record, bytes):
                continue
            uid, received = _UID.search(record), _INTERNAL_DATE.search(record)
            if uid and received and int(uid[1]) in requested:
                date, timestamp = _mail_date(received[1].decode("ascii", "replace"), "")
                if not date:
                    raise MailClientError("邮件服务器返回了无效的收件日期，请刷新后重试。")
                dates[int(uid[1])] = timestamp
        _check_missing_uids(client, {uid for uid in batch if uid not in dates})
    return dates


def _date_order(dates):
    return tuple(sorted(dates, key=lambda uid: (dates[uid], uid), reverse=True))


def _check_missing_uids(client, missing):
    # A successful FETCH omits expunged UIDs. Do not quietly skip a live
    # message if the server returned incomplete date metadata or headers.
    if missing:
        data = _ok(client.uid("SEARCH", None, "UID", ",".join(map(str, sorted(missing)))))
        if any(line and line.strip() for line in data):
            raise MailClientError("邮件服务器未返回完整的邮件信息，请刷新后重试。")


def _page_from_cursor(client, account, cursor, size=PAGE_SIZE):
    items = []
    offset = cursor.offset
    while len(items) < size and offset < len(cursor.ordered_uids):
        uids = cursor.ordered_uids[offset:offset + size - len(items)]
        uid_set = ",".join(map(str, uids))
        headers = _ok(client.uid("FETCH", uid_set, _HEADER_FIELDS))
        by_uid = {item.uid: item for item in _mail_headers(account, cursor.validity, headers)}
        _check_missing_uids(client, set(uids).difference(by_uid))
        items.extend(by_uid[uid] for uid in uids if uid in by_uid)
        offset += len(uids)
    items = _with_previews(client, items)
    if offset == len(cursor.ordered_uids):
        return MailPage(items, None)
    return MailPage(items, items[-1].uid, replace(cursor, offset=offset))


def _search_criteria(query):
    criteria = []
    for key, day in (("SINCE", query.since), ("BEFORE", query.before)):
        if day:
            criteria.extend((key, f"{day.day:02}-{imaplib.Months[day.month]}-{day.year:04}"))
    if query.unread_only:
        criteria.append("UNSEEN")
    if query.flagged_only:
        criteria.append("FLAGGED")
    return criteria or ["ALL"]


def _uid_list(data):
    result = []
    for line in data:
        if not line:
            continue
        for token in line.split():
            if not token.isdigit() or len(token) > 10 or not 0 < int(token) < 2**32:
                raise MailClientError("邮箱返回了无效的搜索结果。")
            result.append(int(token))
    return result


def _search_uids(client, criteria, field="", term=""):
    args = list(criteria)
    if term:
        args.append(field)
        if term.isascii():
            args.append('"' + term.replace('\\', '\\\\').replace('"', '\\"') + '"')
        else:
            # imaplib appends one synchronizing literal as the final argument.
            # Separate SEARCHes for FROM and TEXT allow both to use UTF-8 literals.
            args[:0] = ["CHARSET", "UTF-8"]
            client.literal = term.encode("utf-8")
    try:
        kind, data = client.uid("SEARCH", None, *args)
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        return None  # BAD is an exception in imaplib; NO is a normal response.
    finally:
        client.literal = None
    return set(_uid_list(data)) if kind == "OK" else None


def _search_order(client, matches, take):
    if len(matches) <= take:
        # The caller will sort the selected headers by their actual INTERNALDATE.
        return tuple(matches), ""
    notice = ""
    if len(matches) > _SEARCH_DATE_CAP:
        if _has_capability(client, "SORT"):
            try:
                kind, data = client.uid("SORT", "(REVERSE ARRIVAL)", "US-ASCII", "ALL")
                if kind == "OK":
                    ordered = tuple(uid for uid in _uid_list(data) if uid in matches)
                    if set(ordered) == matches:
                        return ordered, ""
            except imaplib.IMAP4.abort:
                raise
            except imaplib.IMAP4.error:
                pass
        notice = f"候选邮件较多，仅按收件日期比较最近写入收件箱的 {_SEARCH_DATE_CAP} 封。"
    uids = sorted(matches)[-_SEARCH_DATE_CAP:]
    return _date_order(_fetch_dates(client, uids)), notice


class MailClient:
    def __init__(self):
        self._tls = ssl.create_default_context()
        self._icloud_login_names: dict[str, str] = {}
        self._date_indexes = OrderedDict()
        self._date_lock = Lock()

    def _inbox_order(self, client, account, validity, count):
        ordered = _server_inbox_order(client, count)
        if ordered is not None:
            return ordered

        # UIDs and INTERNALDATE stay paired throughout one UIDVALIDITY generation.
        # Refresh membership first: an old message imported today may have a large UID.
        key = (account.address, validity)
        live = sorted(set(_uid_list(_ok(client.uid("SEARCH", None, "ALL")))))
        with self._date_lock:
            previous = self._date_indexes.get(key, {})
        dates = {uid: previous[uid] for uid in live if uid in previous}
        dates.update(_fetch_dates(client, [uid for uid in live if uid not in previous]))
        if len(dates) <= _DATE_CACHE_LIMIT:
            snapshot = dates
        else:
            snapshot = {uid: dates[uid] for uid in live[-_DATE_CACHE_LIMIT:] if uid in dates}
        # Publish a completed snapshot. A failed or concurrent fetch cannot mutate
        # an existing reader's index. Evicted dates are simply fetched again.
        if snapshot:
            with self._date_lock:
                self._date_indexes[key] = snapshot
                self._date_indexes.move_to_end(key)
                while sum(map(len, self._date_indexes.values())) > _DATE_CACHE_LIMIT:
                    self._date_indexes.popitem(last=False)
        return _date_order(dates)

    @staticmethod
    def _provider(account):
        try:
            return MAIL_PROVIDERS[account.provider]
        except KeyError:
            raise MailClientError("请选择支持的邮箱服务商。") from None

    def _imap_login(self, client, account, secret):
        names = (account.address,)
        if account.provider == "icloud":
            local = account.address.partition("@")[0]
            preferred = self._icloud_login_names.get(account.address, local)
            names = (preferred, account.address if preferred == local else local)
        for username in names:
            try:
                _ok(client.login(username, secret))
            except imaplib.IMAP4.abort:
                raise
            except imaplib.IMAP4.error as error:
                reason = error.args[0] if error.args else ""
                if isinstance(reason, bytes):
                    reason = reason.decode("ascii", errors="replace")
                if username == names[-1] or not _LOGIN_REJECTION.match(str(reason)):
                    label = self._provider(account).credential_label
                    raise MailClientError(f"IMAP 登录失败，请核对邮箱地址和{label}，并确认已开启 IMAP 服务。") from None
            else:
                if username != names[0]:
                    # Avoid a rejected login on every subsequent iCloud inbox poll.
                    self._icloud_login_names[account.address] = username
                return

    @contextmanager
    def _inbox(self, account, secret, *, readonly=True):
        provider = self._provider(account)
        client = None
        try:
            client = imaplib.IMAP4_SSL(provider.imap_host, 993, ssl_context=self._tls, timeout=_TIMEOUT)
            self._imap_login(client, account, secret)
            if provider.netease_id:
                _ok(client.xatom("ID", '("name" "MailDesk" "version" "1.0")'))
            selected = _ok(client.select("INBOX", readonly=readonly))
            count = int(selected[0])
            _, values = client.response("UIDVALIDITY")
            validity = values[0] if values else None
            if not isinstance(validity, bytes) or not validity.isdigit():
                raise MailClientError("无法确认收件箱标识，请刷新后重试。")
            yield client, count, validity.decode("ascii")
        except imaplib.IMAP4.readonly:
            raise MailClientError("收件箱当前只读，无法保存已读标记，请稍后重试。") from None
        except (OSError, imaplib.IMAP4.error, EOFError, ValueError, TypeError, IndexError):
            raise MailClientError("无法读取 IMAP 收件箱，请检查网络、服务开关及授权码。") from None
        finally:
            if client is not None:
                with suppress(OSError, imaplib.IMAP4.error, EOFError):
                    client.logout()

    @contextmanager
    def _smtp_session(self, account, secret):
        provider = self._provider(account)
        use_starttls = provider.smtp_port == 587
        if use_starttls:
            connection = smtplib.SMTP(provider.smtp_host, provider.smtp_port, timeout=_TIMEOUT)
        else:
            connection = smtplib.SMTP_SSL(provider.smtp_host, provider.smtp_port, context=self._tls, timeout=_TIMEOUT)
        with closing(connection) as client:
            if use_starttls:
                try:
                    client.starttls(context=self._tls)
                except (OSError, smtplib.SMTPException):
                    raise MailClientError(f"无法与{provider.name}建立 SMTP 加密连接，请检查网络后重试。") from None
            try:
                client.login(account.address, secret)
            except smtplib.SMTPAuthenticationError:
                raise MailClientError(f"SMTP 登录失败，请检查发信服务及{provider.credential_label}。") from None
            yield client
            # A failed QUIT cannot invalidate a message the server already accepted.
            with suppress(OSError, smtplib.SMTPException):
                client.quit()

    def verify(self, account: Account, secret: str) -> None:
        with self._inbox(account, secret):
            pass
        try:
            with self._smtp_session(account, secret):
                pass
        except (OSError, smtplib.SMTPException, ValueError, UnicodeError):
            raise MailClientError("无法验证 SMTP 服务，请检查网络及发信服务设置。") from None

    def list_messages(self, account: Account, secret: str, before_uid: InboxCursor | int | None = None) -> MailPage:
        if isinstance(before_uid, InboxCursor) and before_uid.account != account.address:
            raise MailClientError("分页位置属于另一个邮箱，请刷新邮件列表。")
        with self._inbox(account, secret) as (client, count, validity):
            if isinstance(before_uid, InboxCursor):
                if before_uid.validity != validity:
                    raise MailClientError("收件箱标识已变化，请刷新邮件列表。")
                return _page_from_cursor(client, account, before_uid)
            if before_uid is None and count > PAGE_SIZE:
                cursor = InboxCursor(account.address, validity, self._inbox_order(client, account, validity, count), 0)
                return _page_from_cursor(client, account, cursor)
            # Small inboxes fit in one request. Integer anchors retain their
            # original meaning for callers explicitly paging by UID.
            end = count if before_uid is None else _last_sequence_before(client, count, before_uid)
            if end == 0:
                return MailPage([], None)
            start = max(1, end - PAGE_SIZE + 1)
            data = _ok(client.fetch(f"{start}:{end}", _HEADER_FIELDS))
            items = _mail_headers(account, validity, data)
            if before_uid is not None:
                items = [item for item in items if item.uid < before_uid]
            items = _with_previews(client, items)
            if before_uid is None:
                items.sort(key=lambda item: (item.timestamp, item.uid), reverse=True)
            else:
                items.sort(key=lambda item: item.uid, reverse=True)
            return MailPage(items, items[-1].uid if start > 1 and items else None)

    def search_mail(self, account: Account, secret: str, query: MailQuery, limit: int = 10) -> MailSearchResult:
        criteria = _search_criteria(query)
        with self._inbox(account, secret) as (client, count, validity):
            if not count:
                return MailSearchResult([], False)
            matches = None
            fallback = {}
            for field, term in (("FROM", query.sender), ("TEXT", query.text)):
                if term:
                    found = _search_uids(client, criteria, field, term)
                    if found is None:
                        fallback["sender" if field == "FROM" else "text"] = term
                    else:
                        matches = found if matches is None else matches & found
            if matches is None:
                matches = _search_uids(client, criteria)
            if matches is None:
                raise MailClientError("邮箱服务器未能完成搜索，请缩小日期范围后重试。")
            if not matches:
                return MailSearchResult([], False)
            take = _SEARCH_PREVIEW_CAP if fallback else limit
            uids, notice = _search_order(client, matches, take)
            page = _page_from_cursor(client, account, InboxCursor(account.address, validity, uids, 0), take)
            items = sorted(page.items, key=lambda item: (item.timestamp, item.uid), reverse=True)
            partial = bool(notice or page.next_cursor)
            if fallback:
                local = MailQuery(**fallback)
                fields = "发件人、收件人、标题和可用预览" if local.text else "发件人"
                notice += f"服务器未支持指定的搜索条件，已在本地检查 {len(items)} 封候选邮件的{fields}。"
                partial = partial or bool(local.text)
                items = [item for item in items if local.matches_content(item, item.preview)]
            return MailSearchResult(items[:limit], partial or len(items) > limit, notice)

    def scan_arrivals(self, account: Account, secret: str, validity: str | None, after_uid: int) -> ArrivalBatch:
        with self._inbox(account, secret) as (client, count, current_validity):
            latest = _sequence_uid(client, count) if count else 0
            if current_validity != validity:
                return ArrivalBatch(current_validity, latest, [])
            if after_uid >= latest:
                return ArrivalBatch(current_validity, after_uid, [])

            # Restrict SEARCH to a page of actual messages even if UID numbers
            # are sparse or a mailbox has accumulated thousands of arrivals.
            upper = latest
            if count > PAGE_SIZE and latest - after_uid > PAGE_SIZE:
                old_count = _last_sequence_before(client, count, after_uid + 1)
                if old_count + PAGE_SIZE < count:
                    upper = _sequence_uid(client, old_count + PAGE_SIZE)
            if upper <= after_uid:
                raise MailClientError("收件箱正在变化，请稍后重新检查。")
            data = _ok(client.uid("SEARCH", None, "UID", f"{after_uid + 1}:{upper}"))
            uids = sorted({int(token) for line in data if line for token in line.split()})[:PAGE_SIZE]
            if not uids or uids[0] <= after_uid or uids[-1] > upper:
                raise MailClientError("收件箱正在变化，请稍后重新检查。")
            uid_set = ",".join(map(str, uids))
            headers = _ok(client.uid("FETCH", uid_set, _HEADER_FIELDS))
            items = sorted(_mail_headers(account, current_validity, headers), key=lambda item: item.uid)
            if [item.uid for item in items] != uids:
                raise MailClientError("本次检查未取得完整的邮件列表，请稍后重试。")
            return ArrivalBatch(current_validity, uids[-1], _with_previews(client, items, required=True))

    @contextmanager
    def _message_inbox(self, account: Account, secret: str, item: MailItem, *, readonly=True):
        if (item.account.casefold() != account.address.casefold()
                or type(item.uid) is not int or not 1 <= item.uid < 2**32):
            raise MailClientError("邮件与所选邮箱不匹配，请刷新邮件列表。")
        with self._inbox(account, secret, readonly=readonly) as (client, _, validity):
            if validity != item.validity:
                raise MailClientError("收件箱标识已变化，请刷新邮件列表。")
            yield client

    def mark_read(self, account: Account, secret: str, item: MailItem) -> None:
        with self._message_inbox(account, secret, item, readonly=False) as client:
            _, permanent = client.response("PERMANENTFLAGS")
            if permanent and isinstance(permanent[-1], bytes):
                advertised = permanent[-1].strip()
                if advertised.startswith(b"(") and b"\\seen" not in advertised.strip(b"()").lower().split():
                    raise MailClientError("邮箱服务器不允许永久保存已读标记。")
            uid = str(item.uid)
            _ok(client.uid("STORE", uid, "+FLAGS.SILENT", r"(\Seen)"))
            # A STORE for a vanished UID can also return OK. Confirm the target's flags.
            flags_data = _ok(client.uid("FETCH", uid, "(UID FLAGS)"))
            for line in reversed(flags_data):
                if isinstance(line, bytes):
                    matched, flags = _UID.search(line), _FLAGS.search(line)
                    if matched and int(matched[1]) == item.uid and flags:
                        if b"\\seen" in flags[1].lower().split():
                            return
                        break
            raise MailClientError("服务器未确认这封邮件的已读标记，请刷新后重试。")

    def read_body(self, account: Account, secret: str, item: MailItem) -> str:
        return self._read_message(account, secret, item, rich=False).text

    def read_content(self, account: Account, secret: str, item: MailItem) -> MailBody:
        return self._read_message(account, secret, item, rich=True)

    def send_reply(self, account: Account, secret: str, envelope: ReplyEnvelope, text: str) -> None:
        message = reply_message(account, envelope, text)
        submitting = False
        try:
            with self._smtp_session(account, secret) as client:
                submitting = True
                refused = client.send_message(message, from_addr=account.address, to_addrs=[envelope.recipient])
                if refused:
                    raise MailClientError("邮箱服务器拒绝了回复地址。")
        except smtplib.SMTPRecipientsRefused:
            raise MailClientError("邮箱服务器拒绝了回复地址。") from None
        except (smtplib.SMTPResponseException, smtplib.SMTPNotSupportedError, UnicodeError):
            raise MailClientError("邮箱服务器未接受这封回复，请检查收发件地址及发信权限。") from None
        except (OSError, smtplib.SMTPException):
            if submitting:
                raise ReplyDeliveryUncertain("发信连接中断，发送结果未知。请先在邮箱核实，以免重复发送。") from None
            raise MailClientError("连接发件服务器失败，请检查网络后重试。") from None

    def _read_message(self, account, secret, item, *, rich):
        with self._message_inbox(account, secret, item) as client:
            data = _ok(client.uid("FETCH", str(item.uid), f"(UID RFC822.SIZE BODY.PEEK[]<0.{_BODY_LIMIT}>)"))
            for metadata, raw in _fetch_literals(data):
                matched = _UID.search(metadata)
                if matched is None or int(matched[1]) != item.uid:
                    continue
                size = re.search(rb"\bRFC822\.SIZE\s+(\d+)", metadata, re.I)
                truncated = int(size[1]) > len(raw) if size else len(raw) >= _BODY_LIMIT
                try:
                    body = _parse_body(raw, rich=rich, partial=truncated)
                    if truncated:
                        separator = re.search(rb"\r?\n\r?\n", raw)
                        root = BytesHeaderParser(policy=policy.default).parsebytes(raw[:separator.start()] if separator else b"")
                        if rich and root.get_content_type() in _IMAGE_TYPES and not root.is_attachment():
                            total = int(size[1]) if size else None
                            if total is None or total <= _RICH_BODY_LIMIT:
                                # A single image has no multipart boundary; join the exact
                                # remaining octets before decoding its transfer encoding.
                                limit = (total if total is not None else _RICH_BODY_LIMIT + 1) - len(raw)
                                tail = _fetch_part(client, item.uid, "", limit, offset=len(raw))
                                if tail and (len(tail) == limit if total is not None else len(tail) < limit):
                                    return _parse_body(raw, tail, rich=True)
                            return MailBody("这封图片邮件超过预览限制，或尚未完整载入。", reply=body.reply)
                        missing = {src for src in _parse_html(body.html).images
                                   if src not in body.inline_images and not src.lower().startswith("data:")}
                        if (not body.text and not body.html) or (
                                rich and root.get_content_maintype() == "multipart" and (not body.html or missing)):
                            body = _find_late_body(client, item.uid, body, rich, _document_base(root))
                            if not rich:
                                return body if body.text else MailBody("这封邮件较大，在预览范围内未找到可显示的正文。")
                        notice = ("（邮件较大，部分正文或图片可能未显示。）" if rich
                                  else "（邮件较大，仅预览前 2 MiB 内可解析的正文。）")
                        body = replace(body, text=body.text + "\n\n" + notice,
                                       html=body.html + "<p>" + notice + "</p>" if body.html else "")
                except (RecursionError, ValueError, TypeError):
                    raise MailClientError("邮件格式异常，无法预览正文。") from None
                return body if body.text or body.html else replace(body, text="这封邮件没有可预览的文本正文。")
        raise MailClientError("这封邮件已移走或删除，请刷新邮件列表。")
