import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from state import GuildState, ResourceRecord, SQLiteStateStore, StateError


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteStateStore(Path(self.tmp.name) / 'data' / 'state.sqlite3')

    def test_missing_file_read_is_empty_without_creating_directory(self):
        self.assertEqual(self.store.load(100), GuildState(100))
        self.assertFalse(self.store.path.parent.exists())

    def test_round_trip_and_independent_guild_updates(self):
        first, second = self.store.load(100), self.store.load(200)
        first.resources['channel:chat'] = ResourceRecord('channel', 111)
        first.resources['message:nickname_guide'] = ResourceRecord('message', 112, channel_id=111)
        first.installed = True
        second.resources['role:viewer'] = ResourceRecord('role', 222)
        self.store.save(first)
        self.store.save(second)
        reopened = SQLiteStateStore(self.store.path)
        self.assertEqual(reopened.load(100), first)
        self.assertEqual(reopened.load(200), second)
        self.assertEqual(reopened.load(300), GuildState(300))

    def test_read_existing_database_does_not_change_bytes_or_timestamp(self):
        state = self.store.load(100)
        self.store.save(state)
        before = self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns
        self.store.load(100)
        self.store.load(200)
        self.assertEqual((self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns), before)

    def test_stale_save_is_rejected_without_overwriting_newer_state(self):
        first, stale = self.store.load(100), self.store.load(100)
        first.installed = True
        self.store.save(first)
        with self.assertRaises(StateError):
            self.store.save(stale)
        self.assertTrue(self.store.load(100).installed)

    def test_corrupt_or_unknown_version_never_becomes_an_empty_installation(self):
        self.store.path.parent.mkdir()
        self.store.path.write_text('not a database')
        with self.assertRaises(StateError):
            self.store.load(100)
        self.store.path.unlink()
        self.store.save(GuildState(100))
        with sqlite3.connect(self.store.path) as db:
            db.execute('UPDATE guild_state SET payload=?', (json.dumps({'version': 999}),))
        with self.assertRaises(StateError):
            self.store.load(100)

    def test_wrong_guild_payload_is_rejected(self):
        self.store.save(GuildState(100))
        with sqlite3.connect(self.store.path) as db:
            db.execute('UPDATE guild_state SET payload=?', (json.dumps({'version': 1, 'guild_id': 200,
                        'installed': True, 'resources': {}}),))
        with self.assertRaises(StateError):
            self.store.load(100)
