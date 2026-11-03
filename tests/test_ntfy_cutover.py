"""Migration barriers and selective rollback: only temporary files/databases."""
import contextlib
from contextlib import closing
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ntfy_cutover',ROOT/'deployment/ntfy_cutover.py')
cutover=importlib.util.module_from_spec(spec);spec.loader.exec_module(cutover)


class CutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.outbox=self.root/'outbox.sqlite3';self.private=self.root/'private';self.private.mkdir()
        self.state=self.root/'bridge-state.json';self.state.write_text(json.dumps({'pending':[]}))
        with closing(sqlite3.connect(self.outbox)) as db, db:db.execute('CREATE TABLE jobs (id TEXT,channel TEXT,payload TEXT,status TEXT)')
        self.calls=[]
        self.patches=[patch.object(cutover,'OUTBOX',self.outbox),patch.object(cutover,'PRIVATE',self.private),
                      patch.object(cutover,'BRIDGE_STATE',self.state),patch.object(cutover,'healthy_snapshot',return_value={}),
                      patch.object(cutover,'systemctl',side_effect=lambda *args:self.calls.append(args))]
        for item in self.patches:item.start()
    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    def test_only_activate_after_legacy_stopped_and_disabled(self):
        cutover.handover()
        self.assertEqual(self.calls,[('stop','piwatch-ntfy'),('disable','piwatch-ntfy')])
        self.assertTrue((self.private/'active').is_file())

    def test_incident_during_staging_aborts_without_stopping_bridge(self):
        with closing(sqlite3.connect(self.outbox)) as db, db:db.execute("INSERT INTO jobs VALUES ('new','ntfy','{}','pending')")
        with self.assertRaisesRegex(RuntimeError,'incident occurred'):cutover.handover()
        self.assertFalse((self.private/'active').exists());self.assertEqual(self.calls,[])

    def test_legacy_pending_queue_prevents_handover(self):
        self.state.write_text(json.dumps({'pending':[{'message':'retained legacy alert'}]}))
        with self.assertRaises(RuntimeError):cutover.handover()
        self.assertEqual(self.calls,[]);self.assertFalse((self.private/'active').exists())

    def test_contract_migration_copies_only_pi_ntfy_credential(self):
        program=self.root/'bridge.py';credential=self.root/'credentials.json'
        program.write_text("url='https://192.0.2.10:9443/'\na={'topic':'alerts'}\nb={'topic':'heartbeat'}\n")
        credential.write_text(json.dumps({'username':'synthetic-user','password':'synthetic-password','unrelated':'do-not-copy'}))
        with patch.object(cutover,'BRIDGE_PROGRAM',program),patch.object(cutover,'BRIDGE_CREDENTIAL',credential):
            value=cutover.legacy_contract()
        self.assertEqual(value['username'],'synthetic-user');self.assertNotIn('unrelated',value)
        self.assertEqual(value['topic'],'alerts');self.assertEqual(value['heartbeat_topic'],'heartbeat')

    def test_configuration_rollback_preserves_unrelated_concurrent_changes(self):
        settings=self.root/'settings.sqlite3'
        value={'gateways':{'ntfy':{'password':'synthetic-new'},'smtp':{'password':'unrelated-not-copied'}},
               'battery':{'low_voltage_v':3.3},'interval_seconds':7,'network_policy':{'down_after_failures':3}}
        with closing(sqlite3.connect(settings)) as db, db:
            db.execute('CREATE TABLE settings(id INTEGER PRIMARY KEY,revision INTEGER,value TEXT,updated_at REAL)')
            db.execute('INSERT INTO settings VALUES(1,4,?,0)',(json.dumps(value),))
        with patch.object(cutover,'SETTINGS',settings):cutover.restore_configuration({'ntfy':None,'network_policy':None})
        with closing(sqlite3.connect(settings)) as db, db:revision,raw=db.execute('SELECT revision,value FROM settings').fetchone()
        after=json.loads(raw)
        self.assertEqual(revision,5);self.assertEqual(after['interval_seconds'],7)
        self.assertEqual(after['gateways']['smtp'],value['gateways']['smtp'])
        self.assertEqual(after['battery'],value['battery']);self.assertNotIn('ntfy',after['gateways'])
