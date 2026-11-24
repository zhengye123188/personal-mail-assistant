import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from maildesk.agent_store import HISTORY_LIMIT, AgentPreferences, AgentStore
from maildesk.mail_chat import ChatTurn
from maildesk.models import Account, MailItem


class AgentStoreTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "agent.sqlite3"
        self.store = AgentStore(self.path)
        self.a = Account("a@qq.com", "qq")
        self.b = Account("b@163.com", "163")
        self.mail = MailItem(self.a.address, "101", 32, "提交报名表", "报名小组", self.a.address,
                             "2026-11-24", 1795480000.0, "私人的原始正文", False)
        self.due = datetime(2026, 12, 2, 18, 0, tzinfo=timezone(timedelta(hours=8)))

    def test_preferences_persist_and_change_next_alert(self):
        self.assertEqual(self.store.preferences(), AgentPreferences())
        self.store.add_task("交表", self.due, self.mail)
        now = self.due - timedelta(hours=2)
        self.assertEqual(self.store.claim_due([self.a], now=now), ())
        configured = AgentPreferences("English", "详细", 120, "审稿、会议和需我确认的邮件")
        self.store.set_preferences(configured)
        reopened = AgentStore(self.path)
        self.assertEqual(reopened.preferences(), configured)
        self.assertEqual(reopened.claim_due([self.a], now=now)[0].due_at,
                         datetime(2026, 12, 2, 10, tzinfo=timezone.utc))
        for invalid in (True, -1, 10081):
            with self.assertRaises(ValueError):
                AgentPreferences(reminder_lead_minutes=invalid)
        with self.assertRaises(ValueError):
            AgentPreferences(focus="会" * 201)

    def test_history_uses_exact_account_set_and_discards_private_payloads(self):
        turn = ChatTurn("第二封说什么", "周三前交表", (self.mail,), self.mail, "私密草稿",
                        (self.a.address,))
        self.store.save_turn([self.a, self.b], turn)
        self.assertEqual(self.store.history([self.a]), ())
        self.assertEqual(self.store.history([self.b]), ())
        saved, = AgentStore(self.path).history([self.b.address, self.a.address, self.a.address])
        self.assertEqual(saved.scope, tuple(sorted((self.a.address, self.b.address))))
        self.assertEqual((saved.question, saved.answer), (turn.question, turn.answer))
        header, = saved.sources
        self.assertEqual((header.account, header.uid, header.validity),
                         (self.mail.account, self.mail.uid, self.mail.validity))
        self.assertFalse(header.preview)
        self.assertIsNone(saved.draft_item)
        with closing(sqlite3.connect(self.path)) as db:
            db_text = "".join(db.iterdump())
        self.assertNotIn("私人的原始正文", db_text)
        self.assertNotIn("私密草稿", db_text)
        with self.assertRaises(ValueError):
            self.store.save_turn([self.b], turn)
        with self.assertRaises(ValueError):
            self.store.save_turn([self.b], replace(turn, scope=()))
        self.store.clear_history([self.a])
        self.assertEqual(len(self.store.history([self.a, self.b])), 1)
        self.store.clear_history([self.b, self.a])
        self.assertEqual(self.store.history([self.a, self.b]), ())

    def test_history_rolls_forward_and_bounds_fields(self):
        for n in range(HISTORY_LIMIT + 3):
            self.store.save_turn([self.a], ChatTurn(str(n), "x" * 5000, (self.mail,) * 12))
        turns = self.store.history([self.a])
        self.assertEqual([turn.question for turn in turns], [str(i) for i in range(3, 3 + HISTORY_LIMIT)])
        self.assertLessEqual(len(turns[-1].answer), 1200)
        self.assertEqual(len(turns[-1].sources), 10)

    def test_conversation_pages_keep_full_transcript_while_memory_stays_bounded(self):
        first = self.store.start_conversation([self.a], ChatTurn("  日程\n 确认 ", "第一轮"))
        second = self.store.start_conversation([self.a], ChatTurn("其他事项", "另一个会话"))
        long_answer = "进展" * 900
        for i in range(1, HISTORY_LIMIT + 2):
            self.store.save_turn([self.a], ChatTurn(str(i), long_answer, (self.mail,) * 11), conversation_id=first)
        reopened = AgentStore(self.path)
        newest, other = reopened.list_conversations([self.a])
        self.assertEqual((newest.id, newest.title, newest.turn_count), (first, "日程 确认", HISTORY_LIMIT + 2))
        self.assertEqual((other.id, other.turn_count), (second, 1))
        page = reopened.conversation_page([self.a], first)
        older = reopened.conversation_page([self.a], first, offset=HISTORY_LIMIT)
        self.assertEqual([turn.question for turn in older], ["  日程\n 确认 ", "1"])
        self.assertEqual([turn.question for turn in page], [str(i) for i in range(2, HISTORY_LIMIT + 2)])
        self.assertEqual((page[-1].answer, len(page[-1].sources)), (long_answer, 11))
        self.assertEqual(reopened.conversation_page([self.a], first, offset=HISTORY_LIMIT * 2), ())
        self.assertEqual(len(reopened.history([self.a], conversation_id=first)[-1].answer), 1200)
        self.assertEqual(reopened.history([self.a], conversation_id=second)[0].question, "其他事项")
        with self.assertRaises(ValueError):
            reopened.conversation_page([self.a], first, offset=-1)

    def test_deletion_is_scoped_cascades_and_late_answers_cannot_restore_it(self):
        scope = [self.a, self.b]
        former = self.store.start_conversation(scope, ChatTurn("审稿", "等待确认", (self.mail,)))
        keep = self.store.start_conversation(scope, ChatTurn("旅行", "整理行程"))
        task = self.store.add_task("审稿", self.due, self.mail)
        self.assertEqual(self.store.list_conversations([self.b]), ())
        self.assertEqual(self.store.history([self.b], conversation_id=former), ())
        self.assertEqual(self.store.conversation_page([self.a], former), ())
        self.assertFalse(self.store.delete_conversation([self.a], former))
        with self.assertRaises(ValueError):
            self.store.save_turn([self.a], ChatTurn("错发", "回复"), conversation_id=former)
        self.assertTrue(self.store.delete_conversation(scope[::-1], former))
        self.assertFalse(self.store.delete_conversation(scope, former))
        self.assertEqual(self.store.list_tasks(scope), (task,))
        self.assertEqual([c.id for c in self.store.list_conversations(scope)], [keep])
        # A queued result can arrive after every old thread was deleted and another created.
        self.store.delete_conversation(scope, keep)
        new = self.store.start_conversation(scope, ChatTurn("新会话", "新答复"))
        self.assertNotIn(new, (former, keep))
        with self.assertRaises(ValueError):
            self.store.save_turn(scope, ChatTurn("审稿", "迟到的回复"), conversation_id=former)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT DISTINCT conversation_id FROM conversation_turns").fetchall(), [(new,)])

    def test_legacy_transcripts_migrate_once_during_concurrent_reopens(self):
        legacy_path = self.path.with_name("legacy.sqlite3")
        with closing(sqlite3.connect(legacy_path)) as db, db:
            db.execute("""CREATE TABLE turns(id INTEGER PRIMARY KEY, scope TEXT NOT NULL,
                          question TEXT NOT NULL, answer TEXT NOT NULL, sources TEXT NOT NULL)""")
            db.executemany("INSERT INTO turns VALUES (?,?,?,?,?)", [
                (2, '["a@qq.com"]', "旧问题一", "旧答复一", "[]"),
                (4, '["b@163.com"]', "另一邮箱", "单独保留", "[]"),
                (5, '["a@qq.com"]', "旧问题二", "旧答复二", "[]"),
            ])
        with ThreadPoolExecutor(max_workers=4) as pool:
            opened = list(pool.map(lambda _: AgentStore(legacy_path), range(4)))
        for store in opened:
            migrated, = store.list_conversations([self.a])
            self.assertEqual((migrated.title, migrated.turn_count), ("旧问题一", 2))
            self.assertEqual([turn.answer for turn in store.conversation_page([self.a], migrated.id)],
                             ["旧答复一", "旧答复二"])
            separate, = store.list_conversations([self.b])
            self.assertEqual((separate.title, separate.turn_count), ("另一邮箱", 1))
        with ThreadPoolExecutor(max_workers=4) as pool:
            thread_ids = list(pool.map(lambda n: store.save_turn([self.a], ChatTurn(str(n), "答")), range(12)))
        self.assertEqual(set(thread_ids), {migrated.id})
        self.assertEqual(store.list_conversations([self.a])[0].turn_count, 14)

    def test_confirmations_idempotent_per_source_and_scoped_completion(self):
        other_mail = replace(self.mail, account=self.b.address)
        first = self.store.add_task("Submit   form", self.due, self.mail, "周三 18 点前交表")
        repeated = self.store.add_task("submit form", self.due.astimezone(timezone.utc), self.mail)
        self.assertEqual(first, repeated)
        other = self.store.add_task("Submit form", self.due, other_mail)
        new_generation = self.store.add_task("Submit form", self.due, replace(self.mail, validity="102"))
        self.assertNotEqual(first.id, new_generation.id)
        self.assertFalse(self.store.complete_task(first.id, [self.b]))
        self.assertTrue(self.store.complete_task(first.id, [self.a]))
        self.assertFalse(self.store.complete_task(first.id, [self.a]))
        self.assertEqual(self.store.list_tasks([self.a]), (new_generation,))
        records = self.store.list_tasks([self.a, self.b], include_completed=True)
        self.assertEqual(self.store.list_tasks([self.a, self.b], limit=1), (other,))
        self.assertEqual([t.id for t in records], [other.id, new_generation.id, first.id])
        self.assertTrue(records[-1].completed)
        self.assertEqual(self.store.claim_due([self.a], now=self.due), (new_generation,))
        self.assertEqual(self.store.claim_due([self.b], now=self.due), (other,))

    def test_concurrent_restart_coalesces_overdue_reminder(self):
        overdue = self.store.add_task("交表", self.due, self.mail)
        now = self.due + timedelta(days=2)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: AgentStore(self.path).claim_due([self.a], now=now), range(4)))
        self.assertEqual([task for batch in results for task in batch], [overdue])
        reopened = AgentStore(self.path)
        self.assertEqual(reopened.claim_due([self.a], now=now), ())
        self.assertEqual(reopened.list_tasks([self.a]), (overdue,))

    def test_removal_forgets_mixed_conversations_but_keeps_other_mailbox(self):
        for scope in ([self.a, self.b], [self.a], [self.b]):
            self.store.save_turn(scope, ChatTurn("问题", "回答"))
        self.store.add_task("交表", self.due, self.mail)
        keep = self.store.add_task("缴费", self.due, replace(self.mail, account=self.b.address))
        self.store.forget_account(self.a.address)
        self.assertFalse(self.store.history([self.a, self.b]))
        self.assertFalse(self.store.history([self.a]))
        self.assertTrue(self.store.history([self.b]))
        self.assertEqual(self.store.list_tasks([self.a, self.b]), (keep,))

    def test_requires_identifiable_source_and_unambiguous_due_time(self):
        for title, when, src in ((" ", self.due, self.mail), ("交表", self.due.replace(tzinfo=None), self.mail),
                                 ("交表", self.due, replace(self.mail, validity=""))):
            with self.assertRaises(ValueError):
                self.store.add_task(title, when, src)
        self.assertEqual(self.store.list_tasks([self.a]), ())
