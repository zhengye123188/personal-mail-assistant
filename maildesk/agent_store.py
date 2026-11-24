"""Local preferences, mailbox-scoped conversation memory and confirmed deadlines."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .models import Account, MailItem

if TYPE_CHECKING:
    from .mail_chat import ChatTurn


LANGUAGE_OPTIONS = ("跟随提问", "中文", "English")
DETAIL_OPTIONS = ("简洁", "详细")
HISTORY_LIMIT = 8


@dataclass(frozen=True)
class Conversation:
    id: int
    title: str
    turn_count: int


@dataclass(frozen=True)
class AgentPreferences:
    language: str = LANGUAGE_OPTIONS[0]
    detail: str = DETAIL_OPTIONS[0]
    reminder_lead_minutes: int = 30
    focus: str = ""

    def __post_init__(self):
        if self.language not in LANGUAGE_OPTIONS or self.detail not in DETAIL_OPTIONS:
            raise ValueError("请选择有效的回答语言和详略程度。")
        if type(self.reminder_lead_minutes) is not int or not 0 <= self.reminder_lead_minutes <= 10080:
            raise ValueError("提前提醒时间应在 0 至 10080 分钟之间。")
        if not isinstance(self.focus, str) or len(self.focus) > 200:
            raise ValueError("关注主题应在 200 字以内。")


@dataclass(frozen=True)
class AgentTask:
    id: int
    title: str
    due_at: datetime
    source: MailItem
    evidence: str
    completed: bool


def _scope_addresses(scope):
    if isinstance(scope, (str, Account)):
        scope = (scope,)
    return tuple(sorted({item.address if isinstance(item, Account) else item for item in scope}))


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _source_header(item):
    # Source links can reopen live mail by account, UIDVALIDITY and UID.
    return {
        "account": item.account, "validity": item.validity, "uid": item.uid,
        "subject": item.subject[:300], "sender": item.sender[:200], "recipient": item.recipient[:200],
        "date": item.date[:100], "timestamp": item.timestamp,
    }


def _utc_seconds(moment):
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError("截止时间需要包含时区。")
    return int(moment.timestamp())


def _excerpt(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _conversation_title(question):
    return _excerpt(" ".join(question.split()), 50) or "新对话"


def _task(row):
    return AgentTask(
        row["id"], row["title"], datetime.fromtimestamp(row["due"], timezone.utc),
        MailItem(**json.loads(row["source"])), row["evidence"], bool(row["completed"]),
    )


class AgentStore:
    def __init__(self, path):
        self.path = Path(path)
        with closing(self._connect()) as db, db:
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS preferences (
                    id INTEGER PRIMARY KEY CHECK(id = 1), language TEXT NOT NULL,
                    detail TEXT NOT NULL, lead_minutes INTEGER NOT NULL, focus TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, title TEXT NOT NULL,
                    last_turn_id INTEGER NOT NULL DEFAULT 0, turn_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS conversations_by_scope ON conversations(scope,last_turn_id);
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    question TEXT NOT NULL, answer TEXT NOT NULL, sources TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS turns_by_conversation ON conversation_turns(conversation_id,id);
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL,
                    validity TEXT NOT NULL, uid INTEGER NOT NULL, title TEXT NOT NULL,
                    title_key TEXT NOT NULL, due INTEGER NOT NULL, source TEXT NOT NULL,
                    evidence TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
                    notified INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(account, validity, uid, title_key, due)
                );
                CREATE INDEX IF NOT EXISTS pending_deadlines ON tasks(account, due)
                    WHERE completed=0 AND notified=0;
            """)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turns'").fetchone():
                # Legacy releases kept one rolling transcript per exact scope.
                # Move each transcript and retire its old table in the same transaction.
                for row in db.execute("""SELECT scope,question FROM turns WHERE id IN
                                         (SELECT MIN(id) FROM turns GROUP BY scope) ORDER BY id"""):
                    thread = db.execute("INSERT INTO conversations(scope,title) VALUES (?,?)",
                                        (row["scope"], _conversation_title(row["question"]))).lastrowid
                    copied = db.execute("""INSERT INTO conversation_turns(conversation_id,question,answer,sources)
                                           SELECT ?,question,answer,sources FROM turns WHERE scope=? ORDER BY id""",
                                        (thread, row["scope"]))
                    db.execute("UPDATE conversations SET last_turn_id=?,turn_count=? WHERE id=?",
                               (copied.lastrowid, copied.rowcount, thread))
                db.execute("DROP TABLE turns")

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = sqlite3.Row
        return db

    def preferences(self) -> AgentPreferences:
        with closing(self._connect()) as db:
            row = db.execute("SELECT language,detail,lead_minutes,focus FROM preferences WHERE id=1").fetchone()
            return AgentPreferences(*row) if row else AgentPreferences()

    def set_preferences(self, preferences: AgentPreferences):
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO preferences(id,language,detail,lead_minutes,focus) VALUES (1, ?, ?, ?, ?)",
                       (preferences.language, preferences.detail, preferences.reminder_lead_minutes, preferences.focus))

    def list_conversations(self, scope) -> tuple[Conversation, ...]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT id,title,turn_count FROM conversations WHERE scope=? ORDER BY last_turn_id DESC",
                              (_json(_scope_addresses(scope)),))
            return tuple(Conversation(*row) for row in rows)

    @staticmethod
    def _conversation(db, scope_key, conversation_id):
        if conversation_id is None:
            row = db.execute("SELECT id FROM conversations WHERE scope=? ORDER BY last_turn_id DESC LIMIT 1",
                             (scope_key,)).fetchone()
        else:
            row = db.execute("SELECT id FROM conversations WHERE scope=? AND id=?",
                             (scope_key, conversation_id)).fetchone()
        return row["id"] if row else None

    def history(self, scope, *, conversation_id: int | None = None) -> tuple["ChatTurn", ...]:
        """Small working memory used for model context, independent of the displayed transcript."""
        addresses = _scope_addresses(scope)
        with closing(self._connect()) as db:
            thread = self._conversation(db, _json(addresses), conversation_id)
            return self._read_page(db, addresses, thread, offset=0, compact=True)

    def conversation_page(self, scope, conversation_id: int, *, offset=0) -> tuple["ChatTurn", ...]:
        """Read a page of the full transcript with offset measured back from the newest turn."""
        if type(offset) is not int or offset < 0:
            raise ValueError("对话记录的偏移量必须为非负整数。")
        addresses = _scope_addresses(scope)
        with closing(self._connect()) as db:
            return self._read_page(db, addresses, conversation_id, offset=offset, compact=False)

    @staticmethod
    def _read_page(db, addresses, thread, *, offset, compact):
        from .mail_chat import ChatTurn

        rows = db.execute("""SELECT t.question,t.answer,t.sources FROM conversation_turns t
                             JOIN conversations c ON c.id=t.conversation_id
                             WHERE c.scope=? AND c.id=? ORDER BY t.id DESC LIMIT ? OFFSET ?""",
                          (_json(addresses), thread, HISTORY_LIMIT, offset))
        turns = []
        for row in rows:
            question, answer, sources = row["question"], row["answer"], json.loads(row["sources"])
            if compact:
                question, answer, sources = _excerpt(question, 450), _excerpt(answer, 1200), sources[:10]
            turns.append(ChatTurn(question, answer, tuple(MailItem(**item) for item in sources), scope=addresses))
        return tuple(reversed(turns))

    def start_conversation(self, scope, turn: "ChatTurn") -> int:
        """Persist a new conversation and its first successful exchange together."""
        return self._write_turn(scope, turn, conversation_id=None, start=True)

    def save_turn(self, scope, turn: "ChatTurn", *, conversation_id: int | None = None) -> int:
        """Append to the specified conversation, defaulting to the most recent for legacy callers."""
        return self._write_turn(scope, turn, conversation_id=conversation_id, start=False)

    def _write_turn(self, scope, turn, *, conversation_id, start):
        addresses = _scope_addresses(scope)
        if (not addresses or not set(turn.scope).issubset(addresses)
                or any(item.account not in addresses for item in turn.sources)
                or (turn.draft_item is not None and turn.draft_item.account not in addresses)):
            raise ValueError("对话内容超出当前选择的邮箱范围。")
        scope_key = _json(addresses)
        sources = [_source_header(item) for item in turn.sources]
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            thread = None if start else self._conversation(db, scope_key, conversation_id)
            if thread is None:
                if conversation_id is not None:
                    raise ValueError("当前邮箱范围内找不到这段对话，可能已被删除。")
                thread = db.execute("INSERT INTO conversations(scope,title) VALUES (?,?)",
                                    (scope_key, _conversation_title(turn.question))).lastrowid
            inserted = db.execute("""INSERT INTO conversation_turns(conversation_id,question,answer,sources)
                                     VALUES (?,?,?,?)""",
                                  (thread, turn.question, turn.answer, _json(sources)))
            db.execute("UPDATE conversations SET last_turn_id=?,turn_count=turn_count+1 WHERE id=?",
                       (inserted.lastrowid, thread))
            return thread

    def delete_conversation(self, scope, conversation_id: int) -> bool:
        with closing(self._connect()) as db, db:
            result = db.execute("DELETE FROM conversations WHERE scope=? AND id=?",
                                (_json(_scope_addresses(scope)), conversation_id))
            return result.rowcount > 0

    def clear_history(self, scope):
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM conversations WHERE scope=?", (_json(_scope_addresses(scope)),))

    def add_task(self, title: str, due_at: datetime, source: MailItem, evidence: str = "") -> AgentTask:
        title = " ".join(title.split())
        if not title or len(title) > 200:
            raise ValueError("待办事项名称应在 1 至 200 字之间。")
        if not source.account or not source.validity or type(source.uid) is not int or source.uid <= 0:
            raise ValueError("待办事项需要可识别的来源邮件。")
        key = (source.account, source.validity, source.uid, title.casefold(), _utc_seconds(due_at))
        with closing(self._connect()) as db, db:
            db.execute("""INSERT INTO tasks(account,validity,uid,title_key,due,title,source,evidence)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                          ON CONFLICT(account,validity,uid,title_key,due) DO NOTHING""",
                       (*key, title, _json(_source_header(source)), _excerpt(evidence.strip(), 600)))
            row = db.execute("SELECT * FROM tasks WHERE account=? AND validity=? AND uid=? AND title_key=? AND due=?",
                             key).fetchone()
            return _task(row)

    def list_tasks(self, scope, *, include_completed=False, limit=None) -> tuple[AgentTask, ...]:
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("待办条数须为正整数。")
        addresses = _scope_addresses(scope)
        if not addresses:
            return ()
        placeholders = ",".join("?" for _ in addresses)
        with closing(self._connect()) as db:
            rows = db.execute(f"""SELECT * FROM tasks WHERE account IN ({placeholders}) AND (? OR completed=0)
                                  ORDER BY completed, due, id LIMIT ?""",
                              (*addresses, include_completed, -1 if limit is None else limit))
            return tuple(_task(row) for row in rows)

    def complete_task(self, task_id: int, scope) -> bool:
        addresses = _scope_addresses(scope)
        if not addresses:
            return False
        placeholders = ",".join("?" for _ in addresses)
        with closing(self._connect()) as db, db:
            result = db.execute(f"UPDATE tasks SET completed=1 WHERE id=? AND completed=0 AND account IN ({placeholders})",
                                (task_id, *addresses))
            return result.rowcount > 0

    def claim_due(self, scope, *, now: datetime | None = None) -> tuple[AgentTask, ...]:
        """Atomically claim reminders, including any that became due while the app was closed."""
        addresses = _scope_addresses(scope)
        if not addresses:
            return ()
        instant = now if now is not None else datetime.now(timezone.utc)
        placeholders = ",".join("?" for _ in addresses)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT lead_minutes FROM preferences WHERE id=1").fetchone()
            lead = row[0] if row else AgentPreferences().reminder_lead_minutes
            cutoff = _utc_seconds(instant) + lead * 60
            rows = db.execute(f"""SELECT * FROM tasks WHERE completed=0 AND notified=0 AND due<=?
                                  AND account IN ({placeholders}) ORDER BY due, id LIMIT 20""",
                              (cutoff, *addresses)).fetchall()
            tasks = tuple(_task(row) for row in rows)
            db.executemany("UPDATE tasks SET notified=1 WHERE id=?", ((task.id,) for task in tasks))
            return tasks

    def forget_account(self, address: str):
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM tasks WHERE account=?", (address,))
            db.execute("""DELETE FROM conversations WHERE EXISTS
                          (SELECT 1 FROM json_each(conversations.scope) WHERE value=?)""", (address,))
