from dataclasses import dataclass, field
from datetime import date
from urllib.parse import unquote


PAGE_SIZE = 40
DRAFT_CONTEXT_LIMIT = 32_768


def normalize_cid(source: str) -> str:
    value = source.strip()
    if value[:4].lower() == "cid:":
        return "cid:" + unquote(value[4:]).strip("<>")
    return value


@dataclass(frozen=True)
class ReplyEnvelope:
    recipient: str
    subject: str
    in_reply_to: str = ""
    references: tuple[str, ...] = ()
    source: tuple[str, str, int] | None = None


@dataclass(frozen=True)
class MailBody:
    text: str
    html: str = ""
    inline_images: dict[str, bytes] = field(default_factory=dict)
    base_url: str = ""
    reply: ReplyEnvelope | None = None


@dataclass(frozen=True)
class Account:
    address: str
    provider: str


@dataclass(frozen=True)
class MailItem:
    account: str
    validity: str
    uid: int
    subject: str
    sender: str
    recipient: str
    date: str
    timestamp: float
    preview: str = ""
    unread: bool | None = None
    flagged: bool | None = None


@dataclass(frozen=True)
class ReplyDraft:
    item: MailItem
    envelope: ReplyEnvelope | None
    original_text: str
    text: str
    instructions: str = ""
    updated_at: float = 0.0


@dataclass(frozen=True)
class MailQuery:
    text: str = ""
    sender: str = ""
    since: date | None = None
    before: date | None = None
    unread_only: bool = False
    flagged_only: bool = False

    def __post_init__(self):
        for value in (self.text, self.sender):
            if (not isinstance(value, str) or len(value) > 512
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
                raise ValueError("搜索词无效，请使用 512 字以内的单行文字。")
        if any(d is not None and type(d) is not date for d in (self.since, self.before)):
            raise ValueError("请选择有效的搜索日期。")
        if self.since and self.before and self.since >= self.before:
            raise ValueError("搜索起始日应早于截止日。")

    def matches_content(self, item: MailItem, body: str):
        return (self.sender.casefold() in item.sender.casefold()
                and self.text.casefold() in "\n".join((item.subject, item.sender, item.recipient, body)).casefold())


@dataclass(frozen=True)
class MailSearchResult:
    items: list[MailItem]
    truncated: bool
    notice: str = ""


@dataclass(frozen=True)
class InboxCursor:
    account: str
    validity: str
    ordered_uids: tuple[int, ...]
    offset: int


@dataclass(frozen=True)
class MailPage:
    items: list[MailItem]
    before_uid: int | None
    date_cursor: InboxCursor | None = None

    @property
    def next_cursor(self) -> InboxCursor | int | None:
        return self.date_cursor or self.before_uid


@dataclass(frozen=True)
class ArrivalBatch:
    validity: str
    through_uid: int
    items: list[MailItem]
