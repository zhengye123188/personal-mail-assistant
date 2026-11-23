"""Disposable, bounded local snapshots of inbox lists and recently opened messages."""

import base64
import json
import sqlite3
import time
from contextlib import closing, suppress
from dataclasses import asdict

from .models import PAGE_SIZE, MailBody, MailItem, MailPage, ReplyEnvelope


_CACHE_BYTES = 32 * 1024 * 1024
_ENTRY_BYTES = 2 * 1024 * 1024
_ENTRY_COUNT = 256
_IMAGE_AGE_NS = 24 * 60 * 60 * 10**9


class MailCache:
    def __init__(self, path):
        self.path = path
        # A locked, read-only or damaged cache must never prevent live mail access.
        with suppress(sqlite3.Error), closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    account TEXT PRIMARY KEY, started INTEGER NOT NULL, items TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS seen (
                    account TEXT, validity TEXT, uid INTEGER, at INTEGER NOT NULL,
                    PRIMARY KEY(account, validity, uid)
                );
                CREATE TABLE IF NOT EXISTS assets (
                    account TEXT, validity TEXT, uid INTEGER, part TEXT,
                    payload BLOB NOT NULL, fetched INTEGER NOT NULL, accessed INTEGER NOT NULL,
                    PRIMARY KEY(account, validity, uid, part)
                );
            """)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=0.1)

    def first_page(self, address):
        with suppress(sqlite3.Error, ValueError, KeyError, TypeError), closing(self._connect()) as db:
            row = db.execute("SELECT items FROM snapshots WHERE account=?", (address,)).fetchone()
            if row:
                items = [MailItem(**fields) for fields in json.loads(row[0])]
                if len(items) <= PAGE_SIZE and all(i.account == address for i in items):
                    return MailPage(items, None)
        return None

    def save_page(self, address, page, started):
        items = [asdict(i) for i in page.items[:PAGE_SIZE]]
        if any(i["account"] != address for i in items):
            return
        with suppress(sqlite3.Error), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT started FROM snapshots WHERE account=?", (address,)).fetchone()
            if previous and previous[0] > started:
                return
            # A concurrent IMAP STORE may confirm a read after the list FETCH began.
            seen = set(db.execute("SELECT validity, uid FROM seen WHERE account=? AND at>=?", (address, started)))
            for item in items:
                if (item["validity"], item["uid"]) in seen:
                    item["unread"] = False
            db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)",
                       (address, started, json.dumps(items, ensure_ascii=False)))
            db.execute("DELETE FROM seen WHERE account=? AND at<?", (address, started))
            if not items:
                db.execute("DELETE FROM assets WHERE account=?", (address,))
            else:
                validity = items[0]["validity"]
                db.execute("DELETE FROM assets WHERE account=? AND validity<>?", (address, validity))
                if page.next_cursor is None:
                    uids = [i["uid"] for i in items]
                    placeholders = ",".join("?" for _ in uids)
                    db.execute(f"DELETE FROM assets WHERE account=? AND uid NOT IN ({placeholders})",
                               (address, *uids))

    def mark_read(self, item):
        key = (item.account, item.validity, item.uid)
        with suppress(sqlite3.Error, ValueError, TypeError, KeyError), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?)", (*key, time.time_ns()))
            db.execute("DELETE FROM seen WHERE rowid IN (SELECT rowid FROM seen ORDER BY at DESC LIMIT -1 OFFSET 1024)")
            row = db.execute("SELECT items FROM snapshots WHERE account=?", (item.account,)).fetchone()
            if row:
                fields = json.loads(row[0])
                for entry in fields:
                    if (entry["validity"], entry["uid"]) == key[1:]:
                        entry["unread"] = False
                        db.execute("UPDATE snapshots SET items=? WHERE account=?",
                                   (json.dumps(fields, ensure_ascii=False), item.account))
                        break

    def _payload(self, key, part):
        with suppress(sqlite3.Error), closing(self._connect()) as db, db:
            row = db.execute("SELECT payload, fetched FROM assets WHERE account=? AND validity=? AND uid=? AND part=?",
                             (*key, part)).fetchone()
            now = time.time_ns()
            if row and (not part or now - row[1] < _IMAGE_AGE_NS):
                db.execute("UPDATE assets SET accessed=? WHERE account=? AND validity=? AND uid=? AND part=?",
                           (now, *key, part))
                return row[0]
        return None

    def _save(self, key, parts):
        rows = [(part, data) for part, data in parts.items() if len(data) <= _ENTRY_BYTES]
        if not rows:
            return
        now = time.time_ns()
        with suppress(sqlite3.Error), closing(self._connect()) as db, db:
            db.executemany("INSERT OR REPLACE INTO assets VALUES (?, ?, ?, ?, ?, ?, ?)",
                           [(*key, part, data, now, now) for part, data in rows])
            # length(BLOB) reads the SQLite record size without loading the blob into Python.
            sizes = db.execute("SELECT rowid, length(payload) FROM assets ORDER BY accessed DESC, rowid DESC")
            total, expired = 0, []
            for i, (rowid, size) in enumerate(sizes):
                total += size
                if total > _CACHE_BYTES or i >= _ENTRY_COUNT:
                    expired.append((rowid,))
            db.executemany("DELETE FROM assets WHERE rowid=?", expired)

    def body(self, key):
        data = self._payload(key, "")
        if data:
            with suppress(ValueError, TypeError, KeyError, AttributeError):
                fields = json.loads(data)
                images = {url: base64.b64decode(raw, validate=True) for url, raw in fields["inline_images"].items()}
                reply = fields["reply"]
                if reply:
                    if tuple(reply["source"]) != key:
                        return None
                    reply = ReplyEnvelope(reply["recipient"], reply["subject"], reply["in_reply_to"],
                                          tuple(reply["references"]), key)
                return MailBody(fields["text"], fields["html"], images, fields["base_url"], reply)
        return None

    def save_body(self, key, body):
        if len(body.text) + len(body.html) + sum(map(len, body.inline_images.values())) > _ENTRY_BYTES:
            return
        fields = {"text": body.text, "html": body.html, "base_url": body.base_url,
                  "inline_images": {url: base64.b64encode(raw).decode("ascii") for url, raw in body.inline_images.items()},
                  "reply": asdict(body.reply) if body.reply else None}
        self._save(key, {"": json.dumps(fields, ensure_ascii=False).encode("utf-8")})

    def images(self, key, urls):
        return {url: data for url in urls if (data := self._payload(key, url)) is not None}

    def save_images(self, key, images):
        self._save(key, images)
