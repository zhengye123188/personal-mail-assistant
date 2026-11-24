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
