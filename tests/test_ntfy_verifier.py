"""Read-only Mac/VPS verifier against a synthetic ntfy cache and process metadata."""
from contextlib import closing, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ntfy_verifier',ROOT/'deployment/verify-ntfy-cutover.py')
verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.cache=self.root/'cache.sqlite3';self.config=self.root/'server.yml'
        self.config.write_text("cache-file: '"+str(self.cache)+"'\nvisitor-request-limit-burst: 120\nvisitor-request-limit-replenish: 250ms\nvisitor-request-limit-exempt-hosts: '127.0.0.1,::1'\n")
        self.cmdline=self.root/'cmdline';self.cmdline.write_bytes(b'/usr/bin/ntfy\0serve\0--config\0'+str(self.config).encode()+b'\0')
        self.environ=self.root/'environ';self.environ.write_bytes(b'UNRELATED_PASSWORD=never-output\0')
        self.binary=self.root/'monitor';self.binary.write_bytes(b'synthetic monitor binary')
        now=int(time.time())
        self.proof={'heartbeat_topic':'heartbeat','samples':[{'id':'sample'+str(i),'accepted_at':now-79+i} for i in range(80)],'phone_receipt_confirmed':False}
        with closing(sqlite3.connect(self.cache)) as db,db:
            db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,mid TEXT,time INTEGER,topic TEXT,message TEXT)')
            for sample in self.proof['samples']:
                db.execute('INSERT INTO messages(mid,time,topic,message) VALUES(?,?,?,?)',
                           (sample['id'],sample['accepted_at'],'heartbeat',json.dumps({'piwatch_fresh':True})))
    def tearDown(self):self.temp.cleanup()
    def verify(self):
        scope={};exec(verifier.VPS_CHECK,scope)
        original_path=Path
        def path(value):
            paths={'/proc/101/cmdline':self.cmdline,'/proc/101/environ':self.environ,'/proc/102/exe':self.binary}
            return paths.get(str(value),original_path(value))
        def property_call(args,**kwargs):
            unit=args[2];key=next(arg for arg in args if arg.startswith('--property=')).split('=',1)[1]
            return {'ActiveState':'active','MainPID':'101' if unit=='ntfy-serverwatch' else '102','WorkingDirectory':'/'}[key]
        scope['Path']=path;output=io.StringIO()
        with patch.object(scope['subprocess'],'check_output',side_effect=property_call), \
             patch.object(scope['subprocess'],'run',return_value=SimpleNamespace(returncode=0,stdout='')),redirect_stdout(output):
            scope['main'](self.proof)
        self.assertNotIn('never-output',output.getvalue())
        return json.loads(output.getvalue())
    def test_all_native_ids_and_current_freshness_match_vps_cache(self):
        result=self.verify();self.assertTrue(result['ok'],result)
        self.assertEqual(result['native_samples_verified'],80);self.assertFalse(result['phone_receipt_confirmed'])
    def test_false_freshness_rejects_cutover(self):
        with closing(sqlite3.connect(self.cache)) as db,db:db.execute("UPDATE messages SET message='{}' WHERE mid='sample30'")
        self.assertFalse(self.verify()['ok'])
    def test_restored_default_rate_limit_is_detected_without_modifying_it(self):
        before=self.config.read_text().replace('120','60');self.config.write_text(before)
        self.assertFalse(self.verify()['ok']);self.assertEqual(self.config.read_text(),before)
