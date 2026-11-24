"""Local account metadata and access to mail through OS-managed credentials."""

import json
import re
import sqlite3
import sys
import time
from concurrent.futures import Future
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event, RLock
from uuid import uuid4

from .models import (
    DRAFT_CONTEXT_LIMIT, Account, InboxCursor, MailBody, MailItem, MailPage, MailQuery, MailSearchResult,
    ReplyDraft, ReplyEnvelope,
)
from .mail_cache import MailCache
from .providers import MAIL_PROVIDERS, provider_for_address


_NON_ACTION_SUBJECT = re.compile(
    r"验证码|动态口令|优惠券|限时促销|\b(?:verification code|one[- ]time (?:code|password)|flash sale)\b", re.I,
)
_NEGATED_REQUEST = re.compile(
    r"(?:请勿|无需|无须|不必)(?:再|直接)?(?:回复|答复|确认|处理|反馈)"
    r"|\bno (?:further )?(?:action|response) (?:is )?(?:required|requested)\b", re.I,
)
_ACTION_REQUEST = re.compile(
    r"(?:请|麻烦|烦请|需要你|需要您)[^。！？!?\n]{0,18}?(?:确认|回复|答复|反馈|审批|审核|审阅|评审|提交|签署|签字|填写|处理)"
    r"|(?:待确认|待审批|待审核|待签署|待回复|请问.{0,12}是否方便)"
    r"|\b(?:action required|response requested|rsvp|for your (?:review|approval))\b"
    r"|\b(?:please|could you|can you|kindly)\s+(?:confirm|reply|respond|review|approve|submit|sign|complete|send)\b",
    re.I,
)


def _needs_attention(item: MailItem) -> bool:
    """A flag takes priority; otherwise look for an explicit request in the subject or preview."""
    if item.flagged:
        return True
    if _NON_ACTION_SUBJECT.search(item.subject):
        return False
    text = f"{item.subject}\n{item.preview}"
    return bool(_ACTION_REQUEST.search(_NEGATED_REQUEST.sub("", text)))


class CredentialAccessPaused(RuntimeError):
    def __init__(self):
        super().__init__("系统凭据库访问已暂停。需要联网时，点「刷新」重新授权。")


class MailboxAuthorizationRequired(RuntimeError):
    def __init__(self):
        super().__init__("此邮箱需要钥匙串授权才能同步。")


class KeyringVault:
    """Use the native credential store, including when alternate keyrings are installed."""

    service_name = "personal-mail-assistant"
    # Inbox workers and AI model settings create separate vault instances.
    _access = RLock()
    _session_secrets: dict[str, str] = {}
    _paused = Event()

    @classmethod
    def resume_access(cls) -> None:
        """Allow another system prompt after an explicit user retry."""
        cls._paused.clear()

    def _backend(self):
        try:
            if sys.platform == "darwin":
                from .mac_keychain import MacKeyring as Keyring
            elif sys.platform == "win32":
                from keyring.backends.Windows import WinVaultKeyring as Keyring
            elif sys.platform.startswith("linux"):
                from keyring.backends.SecretService import Keyring
            else:
                raise RuntimeError("当前系统没有受支持的凭据库。")
        except ImportError as exc:
            raise RuntimeError("请安装 keyring 及当前系统的凭据库支持。") from exc
        return Keyring()

    @contextmanager
    def _system_access(self, *, quiet=False):
        backend = self._backend()
        from keyring.errors import KeyringError, KeyringLocked

        try:
            yield backend
        except KeyringError as exc:
            # macOS wraps several Security.framework authorization statuses in KeyringError.
            cause = exc.__cause__
            status = cause.args[0] if cause and cause.args else None
            if isinstance(exc, KeyringLocked) or (sys.platform == "darwin" and status in (-128, -25293, -25308, -25315)):
                if quiet:
                    raise MailboxAuthorizationRequired() from exc
                self._paused.set()
                raise CredentialAccessPaused() from exc
            raise

    def get(self, address: str) -> str | None:
        return self._read(address, quiet=False)

    def get_for_mail(self, address: str, *, fresh=False) -> str | None:
        return self._read(address, quiet=sys.platform == "darwin", fresh=fresh)

    def _read(self, address: str, *, quiet: bool, fresh=False) -> str | None:
        with self._access:
            if not fresh and address in self._session_secrets:
                return self._session_secrets[address]
            if not quiet and self._paused.is_set():
                raise CredentialAccessPaused()
            with self._system_access(quiet=quiet) as backend:
                read = backend.get_password_without_ui if quiet else backend.get_password
                secret = read(self.service_name, address)
            if secret is not None:
                self._session_secrets[address] = secret
            return secret

    def get_for_update(self, address: str) -> str | None:
        # A rebind must respect changes made directly in the system credential store.
        with self._access:
            self._session_secrets.pop(address, None)
            return self.get(address)

    def set(self, address: str, secret: str) -> None:
        with self._access:
            # A backend may modify an item before reporting a write failure.
            self._session_secrets.pop(address, None)
            with self._system_access() as backend:
                backend.set_password(self.service_name, address, secret)
            self._session_secrets[address] = secret

    def delete(self, address: str) -> None:
        with self._access:
            self._session_secrets.pop(address, None)
            with self._system_access() as backend:
                backend.delete_password(self.service_name, address)

    def discard_mail_copy(self, key_ref: str) -> None:
        with self._access:
            self._session_secrets.pop(key_ref, None)
            with self._system_access(quiet=True) as backend:
                backend.delete_password_without_ui(self.service_name, key_ref)


def _account(provider: str, address: str) -> Account:
    provider = provider.strip().lower()
    address = address.strip().lower()
    if provider not in MAIL_PROVIDERS:
        raise ValueError("请选择支持的邮箱服务商。")
    if provider_for_address(address) != provider:
        domains = "、".join(f"@{domain}" for domain in MAIL_PROVIDERS[provider].domains)
        raise ValueError(f"请输入有效的邮箱地址（{domains}）。")
    return Account(address=address, provider=provider)


class MailService:
    def __init__(self, db_path: Path, client, vault):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._client = client
        self._vault = vault
        self._lock = RLock()
        self._content_requests = {}
        self._cache = MailCache(self._db_path.with_suffix(".cache.sqlite3"))
        with closing(self._connect()) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS accounts "
                "(address TEXT PRIMARY KEY NOT NULL, provider TEXT NOT NULL)"
            )
            # Revisions protect an authorization awaiting user input from racing a rebind.
            db.execute("""
                CREATE TABLE IF NOT EXISTS mailbox_key_refs (
                    address TEXT PRIMARY KEY, key_ref TEXT NOT NULL, revision INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS arrival_cursors (
                    account TEXT PRIMARY KEY, validity TEXT NOT NULL, through_uid INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS pending_mail (
                    account TEXT NOT NULL, validity TEXT NOT NULL, uid INTEGER NOT NULL,
                    subject TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
                    date TEXT NOT NULL, timestamp REAL NOT NULL, unread INTEGER, flagged INTEGER,
                    PRIMARY KEY (account, validity, uid)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS reads_ahead_of_cursor (
                    account TEXT NOT NULL, validity TEXT NOT NULL, uid INTEGER NOT NULL,
                    PRIMARY KEY (account, validity, uid)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS reply_drafts (
                    account TEXT NOT NULL, validity TEXT NOT NULL, uid INTEGER NOT NULL,
                    item_json TEXT NOT NULL, envelope_json TEXT,
                    original_text TEXT NOT NULL, text TEXT NOT NULL, instructions TEXT NOT NULL,
                    updated_at REAL NOT NULL, PRIMARY KEY (account, validity, uid)
                )
            """)
            db.commit()

    def _connect(self):
        # SQLite connections belong to the thread that created them.
        return sqlite3.connect(self._db_path)

    def accounts(self) -> list[Account]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT address, provider FROM accounts ORDER BY rowid")
            return [_account(provider, address) for address, provider in rows]

    @staticmethod
    def _key_record(db, address):
        record = db.execute("SELECT key_ref, revision FROM mailbox_key_refs WHERE address=?", (address,)).fetchone()
        return MailService._validate_key_ref(address, *(record or (address, 0)))

    @staticmethod
    def _validate_key_ref(address, key_ref, revision):
        if key_ref != address and not re.fullmatch(rf"mailbox/{re.escape(address)}/[0-9a-f]{{32}}", key_ref):
            raise ValueError("邮箱的本机凭据引用无效。")
        return key_ref, revision

    @staticmethod
    def _save_key_ref(db, address, key_ref):
        db.execute("""
            INSERT INTO mailbox_key_refs (address, key_ref, revision) VALUES (?, ?, 1)
            ON CONFLICT(address) DO UPDATE SET key_ref=excluded.key_ref, revision=revision+1
        """, (address, key_ref))

    def bind(self, provider: str, address: str, secret: str) -> Account:
        account = _account(provider, address)
        if not secret or not secret.strip():
            raise ValueError(f"请输入{MAIL_PROVIDERS[account.provider].credential_label}。")
        if account.provider == "gmail":
            # Google displays the 16-character password in four groups when copying.
            secret = re.sub(r"[ \u00a0\u202f]", "", secret.strip())
            if not re.fullmatch(r"[A-Za-z0-9]{16}", secret):
                raise ValueError("请输入 Google 账号生成的 16 位应用专用密码。")
        self._client.verify(account, secret)
        with self._lock, closing(self._connect()) as db:
            # Reserve the write before changing the vault so competing bindings
            # cannot overwrite a credential while a previous attempt is rolling back.
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO accounts (address, provider) VALUES (?, ?) "
                "ON CONFLICT(address) DO UPDATE SET provider=excluded.provider",
                (account.address, account.provider),
            )
            read_previous = getattr(self._vault, "get_for_update", self._vault.get)
            key_ref, _ = self._key_record(db, account.address)
            previous = read_previous(key_ref)
            try:
                if secret != previous:
                    self._vault.set(key_ref, secret)
                self._save_key_ref(db, account.address, key_ref)
                db.commit()
            except CredentialAccessPaused:
                # A denied system update leaves the original item intact.
                raise
            except Exception:
                # Even a vault API can report failure after changing the item.
                if self._vault.get(key_ref) != previous:
                    if previous is None:
                        self._vault.delete(key_ref)
                    else:
                        self._vault.set(key_ref, previous)
                raise
        return account

    def _require_bound(self, account: Account):
        with closing(self._connect()) as db:
            row = db.execute("""
                SELECT provider, coalesce(key_ref, address), coalesce(revision, 0)
                FROM accounts LEFT JOIN mailbox_key_refs USING(address) WHERE address=?
            """, (account.address,)).fetchone()
            if row is None or row[0] != account.provider or _account(account.provider, account.address) != account:
                raise ValueError("该邮箱账号尚未绑定。")
            return self._validate_key_ref(account.address, row[1], row[2])

    def authorize_mailbox(self, account: Account) -> None:
        """Remember an explicitly selected mailbox after verifying prompt-free access."""
        if sys.platform != "darwin" or not isinstance(self._vault, KeyringVault):
            self._credentials(account)
            return
        with self._lock:
            source, revision = self._require_bound(account)
            # Measure access before asking. An approval may grant the old item
            # to this process temporarily without persisting across launches.
            try:
                self._check_secret(account, self._vault.get_for_mail(source, fresh=True))
                return
            except MailboxAuthorizationRequired:
                pass
            secret = self._check_secret(account, self._vault.get_for_update(source))
            # File-keychain ACLs on credentials created by a different executable
            # may continue requiring approval. Keep the original for that executable.
            replacement = f"mailbox/{account.address}/{uuid4().hex}"
            self._vault.set(replacement, secret)
            try:
                if self._vault.get_for_mail(replacement, fresh=True) != secret:
                    raise RuntimeError("新钥匙串条目未能通过免弹窗读取验证。")
                with closing(self._connect()) as db:
                    db.execute("BEGIN IMMEDIATE")
                    if self._key_record(db, account.address) != (source, revision):
                        raise RuntimeError("此邮箱的授权已在另一处更新，请重新刷新。")
                    self._save_key_ref(db, account.address, replacement)
                    db.commit()
            except Exception:
                self._vault.discard_mail_copy(replacement)
                raise

    def _credentials(self, account: Account) -> str:
        with self._lock:
            key_ref, _ = self._require_bound(account)
            read = getattr(self._vault, "get_for_mail", self._vault.get)
            return self._check_secret(account, read(key_ref))

    @staticmethod
    def _check_secret(account, secret):
        if not secret:
            label = MAIL_PROVIDERS[account.provider].credential_label
            raise ValueError(f"系统凭据库中没有该邮箱的{label}，请重新绑定。")
        return secret

    def list_messages(self, account: Account, before_uid: InboxCursor | int | None = None) -> MailPage:
        started = time.time_ns()
        secret = self._credentials(account)
        page = self._client.list_messages(account, secret, before_uid)
        if before_uid is None:
            self._cache.save_page(account.address, page, started)
        return page

    def cached_first_page(self, account: Account) -> MailPage | None:
        self._require_bound(account)
        return self._cache.first_page(account.address)

    def search_mail(self, account: Account, query: MailQuery, limit: int = 10) -> MailSearchResult:
        if type(limit) is not int or not 1 <= limit <= 40:
            raise ValueError("每个收件箱可返回 1 到 40 封邮件。")
        return self._client.search_mail(account, self._credentials(account), query, limit)

    def read_body(self, account: Account, item: MailItem) -> str:
        if item.account != account.address:
            raise ValueError("该邮件属于另一个邮箱。")
        self._require_bound(account)
        cached = self._cache.body((account.address, item.validity, item.uid))
        if cached is not None:
            return cached.text
        secret = self._credentials(account)
        return self._client.read_body(account, secret, item)

    def read_content(self, account: Account, item: MailItem) -> MailBody:
        if item.account != account.address:
            raise ValueError("该邮件属于另一个邮箱。")
        self._require_bound(account)
        key = (account.address, item.validity, item.uid)
        with self._lock:
            pending = self._content_requests.get(key)
            if pending is None:
                request = self._content_requests[key] = Future()
        if pending is not None:
            return pending.result()
        try:
            body = self._cache.body(key)
            if body is None:
                body = self._client.read_content(account, self._credentials(account), item)
                if body.reply:
                    body = replace(body, reply=replace(body.reply, source=key))
                self._cache.save_body(key, body)
            request.set_result(body)
            return body
        except BaseException as exc:
            request.set_exception(exc)
            raise
        finally:
            with self._lock:
                del self._content_requests[key]

    def read_images(self, item, urls, download, *, retry=False, cancelled):
        key = (item.account, item.validity, item.uid)
        images = {} if retry else self._cache.images(key, urls)
        missing = [url for url in urls if url not in images]
        if missing and not cancelled():
            fresh = download(tuple(missing), cancelled=cancelled)
            self._cache.save_images(key, fresh)
            images.update(fresh)
        return images

    def send_reply(self, account: Account, item: MailItem, envelope: ReplyEnvelope, text: str) -> None:
        if (item.account != account.address or not envelope
                or envelope.source != (account.address, item.validity, item.uid)):
            raise ValueError("回复与所选邮件或发件账号不匹配，请重新打开邮件。")
        if not text.strip():
            raise ValueError("请先填写回复正文。")
        self._client.send_reply(account, self._credentials(account), envelope, text)

    def save_draft(self, draft: ReplyDraft) -> ReplyDraft:
        item = draft.item
        key = (item.account, item.validity, item.uid)
        if draft.envelope and draft.envelope.source != key:
            raise ValueError("草稿的回复地址与原邮件不匹配。")
        if not (draft.text.strip() or draft.instructions.strip()):
            self.delete_draft(item)
            return draft
        # Keep enough of the source for offline editing, including after an inbox UID reset.
        draft = replace(draft, original_text=draft.original_text[:DRAFT_CONTEXT_LIMIT], updated_at=time.time())
        item_json = json.dumps(asdict(item), ensure_ascii=False)
        envelope_json = json.dumps(asdict(draft.envelope), ensure_ascii=False) if draft.envelope else None
        with closing(self._connect()) as db:
            if not db.execute("SELECT 1 FROM accounts WHERE address=?", (item.account,)).fetchone():
                raise ValueError("草稿的发件账号尚未绑定。")
            db.execute("""
                INSERT INTO reply_drafts
                    (account, validity, uid, item_json, envelope_json, original_text, text, instructions, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, validity, uid) DO UPDATE SET
                    item_json=excluded.item_json, envelope_json=excluded.envelope_json,
                    original_text=excluded.original_text, text=excluded.text,
                    instructions=excluded.instructions, updated_at=excluded.updated_at
            """, (*key, item_json, envelope_json, draft.original_text, draft.text, draft.instructions, draft.updated_at))
            db.commit()
        return draft

    def list_drafts(self, *, include_original=True) -> list[ReplyDraft]:
        original = "original_text" if include_original else "''"
        with closing(self._connect()) as db:
            rows = db.execute(f"""
                SELECT item_json, envelope_json, {original}, text, instructions, updated_at
                FROM reply_drafts ORDER BY updated_at DESC, account, validity, uid DESC
            """)
            drafts = []
            for item_json, envelope_json, original, text, instructions, updated_at in rows:
                item = MailItem(**json.loads(item_json))
                envelope = None
                if envelope_json:
                    fields = json.loads(envelope_json)
                    fields["references"] = tuple(fields["references"])
                    fields["source"] = tuple(fields["source"])
                    envelope = ReplyEnvelope(**fields)
                drafts.append(ReplyDraft(item, envelope, original, text, instructions, updated_at))
            return drafts

    def original_for_draft(self, item: MailItem) -> str:
        with closing(self._connect()) as db:
            row = db.execute("SELECT original_text FROM reply_drafts WHERE account=? AND validity=? AND uid=?",
                             (item.account, item.validity, item.uid)).fetchone()
            return row[0] if row else ""

    def delete_draft(self, item: MailItem) -> None:
        with closing(self._connect()) as db:
            db.execute("DELETE FROM reply_drafts WHERE account=? AND validity=? AND uid=?",
                       (item.account, item.validity, item.uid))
            db.commit()

    def mark_read(self, account: Account, item: MailItem) -> None:
        if item.account != account.address:
            raise ValueError("该邮件属于另一个邮箱。")
        secret = self._credentials(account)
        self._client.mark_read(account, secret, item)
        self._cache.mark_read(item)
        key = (account.address, item.validity, item.uid)
        with closing(self._connect()) as db:
            db.execute("UPDATE pending_mail SET unread=0 WHERE account=? AND validity=? AND uid=?", key)
            # A reminder scan may have fetched the old flags but not yet saved the item.
            db.execute("""
                INSERT OR IGNORE INTO reads_ahead_of_cursor (account, validity, uid)
                SELECT account, validity, ? FROM arrival_cursors
                WHERE account=? AND validity=? AND through_uid < ?
            """, (item.uid, *key))
            db.commit()

    @staticmethod
    def _cursor(db, address):
        return db.execute(
            "SELECT validity, through_uid FROM arrival_cursors WHERE account = ?", (address,),
        ).fetchone()

    def poll_pending(self, account: Account) -> list[MailItem]:
        """Scan arrivals without holding a database lock during network I/O."""
        secret = self._credentials(account)
        with closing(self._connect()) as db:
            previous = self._cursor(db, account.address)
        validity, after_uid = previous or (None, 0)
        batch = self._client.scan_arrivals(account, secret, validity, after_uid)
        if not batch.items and previous == (batch.validity, batch.through_uid):
            return []
        new_items = []
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if self._cursor(db, account.address) != previous:
                # A concurrent poll has already committed; its next scan resumes from there.
                return []
            if batch.validity != validity:
                # A new UID generation may reuse all the UIDs from the previous inbox.
                db.execute("DELETE FROM pending_mail WHERE account = ?", (account.address,))
            else:
                if batch.through_uid < after_uid:
                    raise ValueError("收件箱同步位置已变化，请重试。")
                seen = {uid for (uid,) in db.execute(
                    "SELECT uid FROM reads_ahead_of_cursor WHERE account=? AND validity=? AND uid<=?",
                    (account.address, validity, batch.through_uid),
                )}
                for item in batch.items:
                    if (item.account != account.address or item.validity != validity
                            or not after_uid < item.uid <= batch.through_uid):
                        raise ValueError("待处理邮件与当前收件箱的同步范围不符。")
                    if _needs_attention(item):
                        if item.uid in seen:
                            item = replace(item, unread=False)
                        db.execute(
                            "INSERT INTO pending_mail "
                            "(account, validity, uid, subject, sender, recipient, date, timestamp, unread, flagged) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (item.account, item.validity, item.uid, item.subject, item.sender,
                             item.recipient, item.date, item.timestamp, item.unread, item.flagged),
                        )
                        new_items.append(item)
            db.execute(
                "DELETE FROM reads_ahead_of_cursor WHERE account=? AND (validity<>? OR uid<=?)",
                (account.address, batch.validity, batch.through_uid),
            )
            db.execute(
                "INSERT INTO arrival_cursors (account, validity, through_uid) VALUES (?, ?, ?) "
                "ON CONFLICT(account) DO UPDATE SET validity=excluded.validity, through_uid=excluded.through_uid",
                (account.address, batch.validity, batch.through_uid),
            )
            db.commit()
        return new_items

    def list_pending(self) -> list[MailItem]:
        """List outstanding items even when the mail servers are unavailable."""
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM pending_mail ORDER BY timestamp DESC, account, uid DESC")
            items = []
            for row in rows:
                values = dict(row)
                for flag in ("unread", "flagged"):
                    if values[flag] is not None:
                        values[flag] = bool(values[flag])
                items.append(MailItem(**values))
            return items

    def complete_pending(self, item: MailItem) -> None:
        with closing(self._connect()) as db:
            db.execute(
                "DELETE FROM pending_mail WHERE account = ? AND validity = ? AND uid = ?",
                (item.account, item.validity, item.uid),
            )
            db.commit()
