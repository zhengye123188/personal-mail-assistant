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
_TOTAL_HISTORY_LIMIT = 128


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


def _task(row):
    return AgentTask(
        row["id"], row["title"], datetime.fromtimestamp(row["due"], timezone.utc),
        MailItem(**json.loads(row["source"])), row["evidence"], bool(row["completed"]),
    )


class AgentStore:
    def __init__(self, path):
        self.path = Path(path)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS preferences (
                    id INTEGER PRIMARY KEY CHECK(id = 1), language TEXT NOT NULL,
                    detail TEXT NOT NULL, lead_minutes INTEGER NOT NULL, focus TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY, scope TEXT NOT NULL, question TEXT NOT NULL,
                    answer TEXT NOT NULL, sources TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS turns_by_scope ON turns(scope, id);
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

    def _connect(self):
        db = sqlite3.connect(self.path)
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

    def history(self, scope) -> tuple["ChatTurn", ...]:
        from .mail_chat import ChatTurn

        addresses = _scope_addresses(scope)
        with closing(self._connect()) as db:
            rows = db.execute("SELECT question,answer,sources FROM turns WHERE scope=? ORDER BY id",
                              (_json(addresses),))
            return tuple(ChatTurn(row["question"], row["answer"],
                                  tuple(MailItem(**item) for item in json.loads(row["sources"])),
                                  scope=addresses) for row in rows)

    def save_turn(self, scope, turn: "ChatTurn"):
        addresses = _scope_addresses(scope)
        if (not addresses or not set(turn.scope).issubset(addresses)
                or any(item.account not in addresses for item in turn.sources)
                or (turn.draft_item is not None and turn.draft_item.account not in addresses)):
            raise ValueError("对话内容超出当前选择的邮箱范围。")
        scope_key = _json(addresses)
        sources = [_source_header(item) for item in turn.sources[:10]]
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO turns(scope, question, answer, sources) VALUES (?, ?, ?, ?)",
                       (scope_key, _excerpt(turn.question, 450), _excerpt(turn.answer, 1200), _json(sources)))
            db.execute("""DELETE FROM turns WHERE id IN
                          (SELECT id FROM turns WHERE scope=? ORDER BY id DESC LIMIT -1 OFFSET ?)""",
                       (scope_key, HISTORY_LIMIT))
            db.execute("""DELETE FROM turns WHERE id IN
                          (SELECT id FROM turns ORDER BY id DESC LIMIT -1 OFFSET ?)""", (_TOTAL_HISTORY_LIMIT,))

    def clear_history(self, scope):
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM turns WHERE scope=?", (_json(_scope_addresses(scope)),))

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
            db.execute("""DELETE FROM turns WHERE EXISTS
                          (SELECT 1 FROM json_each(turns.scope) WHERE value=?)""", (address,))
