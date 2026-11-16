import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base import LedgerTestBase


class HashChainTests(LedgerTestBase):
    def test_genesis_then_chain_and_categories(self):
        from src.services.ledger import ensure_initialized
        db = self.session()
        ensure_initialized(db)
        db.commit()
        # 无历史审计时 ensure_initialized 不预建锚点；首条业务动作先落创世锚点
        self.record(db, 'a@x', 'dataset.create', {'rows': 10})
        db.commit()
        g = db.get(self.LedgerEntry, 0)
        self.assertEqual(g.action, 'ledger.genesis')
        self.assertEqual(g.prev_hash, '0' * 64)
        self.assertEqual(g.witness_state, 'pending')

        e1 = db.get(self.LedgerEntry, 1)
        self.assertEqual(e1.category, 'import')
        self.assertEqual(e1.prev_hash, g.entry_hash)
        # 哈希可被独立重算
        from src.services.ledger_crypto import entry_hash
        recomputed = entry_hash(1, e1.audit_id, 'a@x', 'dataset.create',
                                'import', {'rows': 10}, e1.at, g.entry_hash)
        self.assertEqual(recomputed, e1.entry_hash)

        self.record(db, 'b@x', 'job.completed', {'id': 7})
        self.record(db, 'c@x', 'job.approve', {'id': 7})
        self.record(db, 'd@x', 'report.delivered', {'delivery': 3})
        db.commit()
        cats = [db.get(self.LedgerEntry, s).category for s in range(2, 5)]
        self.assertEqual(cats, ['trial', 'approval', 'distribution'])

    def test_atomic_rollback_when_ledger_step_fails(self):
        from src.services.ledger import ensure_initialized
        from sqlalchemy import select, func
        from unittest.mock import patch
        db = self.session()
        ensure_initialized(db)
        db.commit()
        audits_before = db.scalar(select(func.count()).select_from(self.Audit))
        ledger_before = db.scalar(select(func.count()).select_from(self.LedgerEntry))

        # 模拟账本追加步骤真实抛错：审计与账本必须一起撤销，不留半截记录
        with patch('src.services.audit.append_entry',
                   side_effect=RuntimeError('ledger storage failure')):
            with self.assertRaises(RuntimeError):
                self.record(db, 'a@x', 'dataset.create', {'rows': 1})
        db.rollback()
        self.assertEqual(db.scalar(select(func.count()).select_from(self.Audit)), audits_before)
        self.assertEqual(db.scalar(select(func.count()).select_from(self.LedgerEntry)), ledger_before)

        # 失败之后同一连接仍可正常记账，证明事务状态被正确清理
        self.record(db, 'b@x', 'dataset.create', {'rows': 2})
        db.commit()
        self.assertEqual(db.scalar(select(func.count()).select_from(self.Audit)),
                         audits_before + 1)

    def test_tampered_entry_detected_and_first_difference_located(self):
        from src.services.ledger import ensure_initialized, verify_chain
        db = self.session()
        ensure_initialized(db)
        for i in range(5):
            self.record(db, f'u{i}@x', 'job.completed', {'id': i})
        db.commit()
        # 改写第 3 条的内容（模拟审计库被人篡改）
        victim = db.get(self.LedgerEntry, 3)
        victim.payload = {'id': 999, 'forged': True}
        db.commit()
        report = verify_chain(db)
        self.assertFalse(report['chain_ok'])
        self.assertEqual(report['first_difference']['seq'], 3)
        self.assertEqual(report['first_difference']['kind'], 'content')

    def test_deleted_entry_detected_as_gap(self):
        from src.services.ledger import ensure_initialized, verify_chain
        db = self.session()
        ensure_initialized(db)
        for i in range(4):
            self.record(db, 'u@x', 'job.approve', {'id': i})
        db.commit()
        db.delete(db.get(self.LedgerEntry, 2))
        db.commit()
        report = verify_chain(db)
        self.assertFalse(report['chain_ok'])
        self.assertEqual(report['first_difference']['kind'], 'gap')

    def test_concurrent_threads_no_fork_no_gap(self):
        """多线程并发记账：序号连续、链完整、无重复哈希。"""
        from src.services.ledger import ensure_initialized, verify_chain
        db = self.session()
        ensure_initialized(db)
        db.commit()
        db.close()

        errors = []

        def worker(prefix, count):
            try:
                s = self.session()
                for i in range(count):
                    self.record(s, f'{prefix}@x', 'dataset.create', {'i': i})
                    s.commit()
                s.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(f't{t}', 25)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors, errors)
        s = self.session()
        report = verify_chain(s)
        self.assertTrue(report['chain_ok'])
        seqs = [e['seq'] for e in report['entries']]
        self.assertEqual(seqs, list(range(0, len(seqs))))
        hashes = [e['entry_hash'] for e in report['entries']]
        self.assertEqual(len(hashes), len(set(hashes)))
        self.assertEqual(len(seqs), 1 + 8 * 25)

    def test_legacy_backfill_marks_old_records_unwitnessed(self):
        """账本上线前的审计行：回填成链但标 legacy，不倒填可信结论。"""
        import time
        from src.models import Audit
        from src.services.ledger import ensure_initialized, verify_chain
        db = self.session()
        for i in range(3):
            db.add(Audit(actor=f'old{i}', action='dataset.create',
                         detail={'legacy': i}, at=time.time() - 100 + i))
        db.commit()
        ensure_initialized(db)
        db.commit()
        report = verify_chain(db)
        self.assertTrue(report['chain_ok'])
        self.assertEqual(report['counts']['legacy'], 4)  # 创世 + 3 条旧审计
        self.assertEqual(report['counts']['witnessed'], 0)
        # 新动作仍是 pending，不会把旧记录“洗白”
        self.record(db, 'new@x', 'job.approve', {'id': 1})
        db.commit()
        self.assertEqual(db.get(self.LedgerEntry, 4).witness_state, 'pending')
        self.assertEqual(db.get(self.LedgerEntry, 1).witness_state, 'legacy')


if __name__ == '__main__':
    unittest.main()
