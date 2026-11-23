"""Offline mailboxes for exploring the very same desktop reader."""

from dataclasses import replace
from datetime import datetime, timedelta
from importlib.util import find_spec
from threading import Lock

from .mail_replies import reply_message
from .models import PAGE_SIZE, ArrivalBatch, MailBody, MailItem, MailPage, MailSearchResult, ReplyEnvelope


if find_spec(".local_demo_data", __package__) is not None:
    from . import local_demo_data
else:
    local_demo_data = None

DEMO_AVAILABLE = local_demo_data is not None
DEMO_ACCOUNTS = local_demo_data.ACCOUNTS if DEMO_AVAILABLE else ()


class DemoVault:
    """Keep demo credentials in memory and isolated from the system keyring."""

    def __init__(self):
        self._codes: dict[str, str] = {}

    def get(self, address):
        return self._codes.get(address)

    def set(self, address, secret):
        self._codes[address] = secret

    def delete(self, address):
        self._codes.pop(address, None)


class DemoClient:
    def __init__(self, *, examples=None, senders=None, arrival_details=None):
        self._local_content = DEMO_AVAILABLE and examples is None
        self._examples = local_demo_data.EXAMPLES if self._local_content else (examples or {})
        self._senders = local_demo_data.SENDERS if DEMO_AVAILABLE and senders is None else (senders or {})
        self._arrival_details = (local_demo_data.ARRIVAL_DETAILS if DEMO_AVAILABLE and arrival_details is None
                                 else (arrival_details or {}))
        self.start_time = datetime.now().astimezone().replace(second=0, microsecond=0)
        self._arrivals: dict[str, list[tuple[MailItem, str]]] = {}
        self._seen: set[tuple[str, int]] = set()
        self.sent_replies = []
        self._lock = Lock()

    def verify(self, account, secret):
        if not secret:
            raise ValueError("请输入演示授权码。")

    def _mailbox(self, account):
        with self._lock:
            messages = list(reversed(self._arrivals.get(account.address, [])))
        for uid, age, sender, subject, body in self._examples.get(account.provider, ()):
            date = self.start_time - timedelta(minutes=age)
            item = MailItem(account.address, "1", uid, subject, sender, account.address,
                            date.strftime("%Y-%m-%d %H:%M"), date.timestamp(),
                            preview=" ".join(body.split()), unread=uid >= 2,
                            flagged=account.provider == "163" and uid == 3)
            messages.append((item, body))
        with self._lock:
            return [(replace(item, unread=False) if (account.address, item.uid) in self._seen else item, body)
                    for item, body in messages]

    def simulate_arrival(self, account):
        sender, task = self._arrival_details[account.provider]
        body = f"你好，\n\n请确认{task}，并在方便时回复我。谢谢！\n\n{sender}"
        with self._lock:
            arrivals = self._arrivals.setdefault(account.address, [])
            number = len(arrivals) + 1
            uid = max((row[0] for row in self._examples.get(account.provider, ())), default=0) + number
            now = datetime.now().astimezone()
            subject = f"请确认{task}" if number == 1 else f"请确认{task}（{number}）"
            item = MailItem(account.address, "1", uid, subject, sender, account.address,
                            now.strftime("%Y-%m-%d %H:%M"), now.timestamp(),
                            preview=" ".join(body.split()), unread=True, flagged=False)
            arrivals.append((item, body))
        return item

    def list_messages(self, account, secret, before_uid=None):
        items = [item for item, _ in self._mailbox(account) if before_uid is None or item.uid < before_uid]
        page = items[:PAGE_SIZE]
        return MailPage(page, page[-1].uid if len(items) > PAGE_SIZE else None)

    def search_mail(self, account, secret, query, limit=10):
        matches = []
        for item, body in self._mailbox(account):
            received = datetime.fromtimestamp(item.timestamp).date()
            if (query.since and received < query.since) or (query.before and received >= query.before):
                continue
            if (query.unread_only and not item.unread) or (query.flagged_only and not item.flagged):
                continue
            sender = f"{item.sender} <{self._senders[item.sender]}@example.org>"
            if query.matches_content(replace(item, sender=sender), body):
                matches.append(item)
        matches.sort(key=lambda item: (item.timestamp, item.uid), reverse=True)
        return MailSearchResult(matches[:limit], len(matches) > limit)

    def scan_arrivals(self, account, secret, validity, after_uid):
        messages = self._mailbox(account)
        if validity != "1":
            return ArrivalBatch("1", messages[0][0].uid if messages else 0, [])
        items = [item for item, _ in reversed(messages) if item.uid > after_uid][:PAGE_SIZE]
        return ArrivalBatch("1", items[-1].uid if items else after_uid, items)

    def _find_message(self, account, item):
        if item.account == account.address and item.validity == "1":
            for message, body in self._mailbox(account):
                if message.uid == item.uid:
                    return message, body
        raise ValueError("找不到这封演示邮件。")

    def read_body(self, account, secret, item):
        _, body = self._find_message(account, item)
        return body

    def read_content(self, account, secret, item):
        text = self.read_body(account, secret, item)
        recipient = self._senders[item.sender] + "@example.org"
        parent = f"<{account.provider}.{item.uid}@example.org>"
        reply = ReplyEnvelope(recipient, "Re: " + item.subject, parent, (parent,))
        if self._local_content:
            return local_demo_data.read_content(account, item, text, reply)
        return MailBody(text, reply=reply)

    def send_reply(self, account, secret, envelope, text):
        message = reply_message(account, envelope, text)
        with self._lock:
            self.sent_replies.append(message)

    def mark_read(self, account, secret, item):
        self._find_message(account, item)
        with self._lock:
            self._seen.add((account.address, item.uid))
