import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
from http.server import ThreadingHTTPServer

import requests

from receipt_maintenance import maintain_receipts
from server import AdapterHandler, PersonalAdapter, digest


class FileStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def read(self, name):
        path = self.directory / (name + '.dpapi')
        return json.loads(path.read_bytes()) if path.exists() else None

    def write(self, name, value):
        (self.directory / (name + '.dpapi')).write_text(json.dumps(value), encoding='utf-8')


class ReceiptMaintenance(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = FileStore(self.root)
        self.now = 1800000000
        self.request_id = str(uuid.uuid4())
        self.body = {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'question'}]}
        self.store.write('chatgpt', {'issuer': 'test', 'client_id': 'test', 'subject': 'test',
            'ext_agent_host_id': 'test', 'scopes': ['chatgpt.tokens.use.direct']})
        self.adapter = PersonalAdapter(self.store, object())
        self.completion = {'id': 'saved-response', 'choices': [{'message': {'content': 'saved answer'}}]}
        self.receipt_name = 'request-' + self.request_id
        self.store.write(self.receipt_name, {'request_hash': digest(self.body), 'outcome': 'received',
            'connection': self.adapter.connection_identity(self.body), 'model': 'test-model',
            'response_id': 'saved-response', 'completion': self.completion})
        self.store.write('tool-test', {'native_prefix': 'retained continuation'})
        self.raw = self.root / ('wire-' + self.request_id + '.events.dpapi')
        self.raw.write_bytes(b'protected raw response')
        os.utime(self.raw, (self.now - 100 * 86400,) * 2)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def report(self):
        return maintain_receipts(self.store, now=self.now)

    def test_archive_preserves_replay_and_continuation_without_new_inference(self):
        before = self.snapshot()
        plan = self.report()
        self.assertEqual(before, self.snapshot())
        result = maintain_receipts(self.store, apply_plan_sha256=plan['plan_sha256'], now=self.now)
        self.assertEqual(result['moved'], 1)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(self.adapter.complete(self.request_id, self.body), self.completion)
        after = self.snapshot()
        self.assertEqual(before[self.receipt_name + '.dpapi'], after[self.receipt_name + '.dpapi'])
        self.assertEqual(before['tool-test.dpapi'], after['tool-test.dpapi'])
        self.assertEqual((self.root / 'raw-archive' / self.raw.name).read_bytes(), b'protected raw response')

    def test_unknown_outcome_is_retained_even_when_old(self):
        receipt = self.store.read(self.receipt_name)
        receipt.update(outcome='unknown')
        self.store.write(self.receipt_name, receipt)
        before = self.snapshot()
        self.assertEqual(self.report()['archive_files'], 0)
        self.assertEqual(before, self.snapshot())

    def test_modified_evidence_invalidates_reviewed_plan(self):
        plan = self.report()
        self.raw.write_bytes(b'changed response')
        os.utime(self.raw, (self.now - 100 * 86400,) * 2)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Archive plan changed'):
            maintain_receipts(self.store, apply_plan_sha256=plan['plan_sha256'], now=self.now)
        self.assertEqual(before, self.snapshot())

    def test_existing_archive_is_not_overwritten(self):
        plan = self.report()
        archive = self.root / 'raw-archive'
        archive.mkdir()
        (archive / self.raw.name).write_bytes(b'earlier evidence')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'already contains'):
            maintain_receipts(self.store, apply_plan_sha256=plan['plan_sha256'], now=self.now)
        self.assertEqual(before, self.snapshot())

    def test_recent_or_incomplete_receipts_are_not_archived(self):
        os.utime(self.raw, (self.now - 10,) * 2)
        self.assertEqual(self.report()['archive_files'], 0)
        os.utime(self.raw, (self.now - 100 * 86400,) * 2)
        receipt = self.store.read(self.receipt_name)
        receipt.pop('completion')
        self.store.write(self.receipt_name, receipt)
        self.assertEqual(self.report()['archive_files'], 0)

    def test_http_maintenance_requires_pairing_and_reviewed_plan(self):
        os.utime(self.raw, (time.time() - 100 * 86400,) * 2)
        self.adapter.account = SimpleNamespace(host={'pairing_token': 'local-test-pairing'})
        server = ThreadingHTTPServer(('127.0.0.1', 0), AdapterHandler)
        server.adapter = self.adapter
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        session = requests.Session()
        session.trust_env = False
        url = f'http://127.0.0.1:{server.server_port}/maintenance/receipts'
        before = self.snapshot()
        try:
            refused = session.post(url, json={}, timeout=5)
            self.assertEqual(refused.status_code, 401)
            self.assertEqual(before, self.snapshot())
            headers = {'Authorization': 'Bearer local-test-pairing'}
            report = session.post(url, json={}, headers=headers, timeout=5)
            self.assertEqual(report.status_code, 200)
            self.assertEqual(report.json()['archive_files'], 1)
            self.assertEqual(before, self.snapshot())
            applied = session.post(url, json={'apply_plan_sha256': report.json()['plan_sha256']},
                                   headers=headers, timeout=5)
            self.assertEqual(applied.status_code, 200)
            self.assertEqual(applied.json()['moved'], 1)
            self.assertEqual(self.adapter.complete(self.request_id, self.body), self.completion)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
            session.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
