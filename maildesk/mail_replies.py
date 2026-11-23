"""Build a single-sender reply from the original message's outer headers."""

import re
from email import policy
from email.errors import MessageError, UndecodableBytesDefect
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .models import Account, ReplyEnvelope


_MESSAGE_ID = re.compile(r"<[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]{1,240}@[a-zA-Z0-9.:[\]-]{1,240}>")
_RE_PREFIX = re.compile(r"^(?:re|回复|答复)\s*[:：]", re.I)


def _usable_address(address):
    return bool(address.username and address.domain and not any(
        ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in address.addr_spec))


def reply_envelope(message) -> ReplyEnvelope | None:
    # An explicit, ambiguous Reply-To must not fall through to a different mailbox.
    field = "Reply-To" if "Reply-To" in message else "From"
    try:
        headers = message.get_all(field, [])
        if len(headers) != 1 or len(headers[0].addresses) != 1:
            return None
        address = headers[0].addresses[0]
        # Chinese display names are sometimes sent as raw UTF-8/GBK. The
        # mailbox may still be unambiguous ASCII even if its display name isn't.
        if not _usable_address(address) or any(not isinstance(d, UndecodableBytesDefect)
                                               for d in headers[0].defects):
            return None
        subject = " ".join(str(message.get("Subject", "")).split()) or "（无主题）"
        if not _RE_PREFIX.match(subject):
            subject = "Re: " + subject
        ids = _MESSAGE_ID.findall(str(message.get("Message-ID", "")))
        parent = ids[0] if len(ids) == 1 else ""
        refs = _MESSAGE_ID.findall(str(message.get("References", "")))
        if not refs:
            previous = _MESSAGE_ID.findall(str(message.get("In-Reply-To", "")))
            if len(previous) == 1:
                refs = previous
        if parent:
            refs.append(parent)
        return ReplyEnvelope(address.addr_spec, subject, parent, tuple(dict.fromkeys(refs)))
    except (ValueError, IndexError, MessageError, RecursionError):
        # Malformed address fields should not prevent the rest of the email from opening.
        return None


def reply_message(account: Account, envelope: ReplyEnvelope, text: str) -> EmailMessage:
    if not text.strip():
        raise ValueError("请先填写回复正文。")
    try:
        address = Address(addr_spec=envelope.recipient)
        if not _usable_address(address) or address.addr_spec != envelope.recipient:
            raise ValueError
    except (ValueError, IndexError, MessageError):
        raise ValueError("原邮件的回复地址无效。") from None
    reply = EmailMessage(policy=policy.SMTP)
    reply["From"] = account.address
    reply["To"] = address
    reply["Subject"] = envelope.subject
    reply["Date"] = formatdate(localtime=True)
    reply["Message-ID"] = make_msgid(domain=account.address.rpartition("@")[2])
    if envelope.in_reply_to:
        reply["In-Reply-To"] = envelope.in_reply_to
    if envelope.references:
        reply["References"] = " ".join(envelope.references)
    reply.set_content(text, charset="utf-8", cte="quoted-printable")
    return reply
