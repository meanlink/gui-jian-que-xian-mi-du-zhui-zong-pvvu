import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base import LedgerTestBase


class WitnessRelayTests(LedgerTestBase):
    def _seed_entries(self, db, n=4):
        from src.services.ledger import ensure_initialized
        ensure_initialized(db)
        for i in range(n):
            self.record(db, f'u{i}@x',
                        ['dataset.create', 'job.create', 'job.completed',
                         'job.approve', 'report.delivered'][i % 5],
                        {'i': i})
        db.commit()

    def test_happy_path_anchor_then_entries_all_witnessed(self):
        self.start_witness()
        db = self.session()
        self._seed_entries(db, 4)
        stats = self.relayer.relay_once(db)
        self.assertEqual(stats['unavailable'], False)
        self.assertEqual(stats['pending'], 0)
        states = [db.get(self.LedgerEntry, s).witness_state for s in range(5)]
        self.assertTrue(all(s == 'witnessed' for s in states))
        # 见证端独立存储里确实另存了 1 锚点 + 4 凭证
        remote = self.wc.fetch_vouchers('unit-node')
        kinds = sorted(v['kind'] for v in remote['vouchers'])
        self.assertEqual(kinds.count('anchor'), 1)
        self.assertEqual(kinds.count('entry'), 4)
        self.assertEqual(remote['tip']['seq'], 4)

    def test_witness_outage_does_not_block_business_and_retries_idempotent(self):
        db = self.session()
        self._seed_entries(db, 3)
        # 见证端完全不可达：业务记账早已提交成功，中继只报告不可用
        self.point_witness_to_dead_port()
        stats = self.relayer.relay_once(db)
        self.assertTrue(stats['unavailable'])
        self.assertEqual(stats['pending'], 4)
        # 业务在断线期间照常产生新操作（0..3 已占用，新条目序号为 4）
        self.record(db, 'live@x', 'report.delivered', {'delivery': 9})
        db.commit()
        self.assertEqual(db.get(self.LedgerEntry, 4).witness_state, 'pending')

        # 恢复后启动见证端，按原序补送
        self.start_witness()
        stats = self.relayer.relay_once(db)
        self.assertFalse(stats['unavailable'])
        self.assertEqual(stats['pending'], 0)
        remote = self.wc.fetch_vouchers('unit-node')
        self.assertEqual(remote['tip']['seq'], 4)
        first_count = len(remote['vouchers'])
        # 再次中继：重试不新增凭证（锚点幂等 + 无 pending 可发）
        stats2 = self.relayer.relay_once(db)
        self.assertEqual(stats2['sent'], 0)
        self.assertEqual(len(self.wc.fetch_vouchers('unit-node')['vouchers']), first_count)

    def test_retry_duplicate_voucher_returns_same_receipt(self):
        self.start_witness()
        db = self.session()
        self._seed_entries(db, 2)
        self.relayer.relay_once(db)
        entry = db.get(self.LedgerEntry, 1)
        # 手工再发同一凭证：见证端必须幂等，返回原回执，不新增
        r1 = self.wc.submit_voucher(
            {'node': 'unit-node', 'seq': 1, 'entry_hash': entry.entry_hash,
             'prev_hash': entry.prev_hash, 'kind': 'entry'})
        r2 = self.wc.submit_voucher(
            {'node': 'unit-node', 'seq': 1, 'entry_hash': entry.entry_hash,
             'prev_hash': entry.prev_hash, 'kind': 'entry'})
        self.assertTrue(r1['deduplicated'])
        self.assertTrue(r2['deduplicated'])
        self.assertEqual(r1['receipt'], r2['receipt'])
        self.assertEqual(r1['witness_seq'], r2['witness_seq'])

    def test_same_seq_different_content_is_rejected(self):
        self.start_witness()
        db = self.session()
        self._seed_entries(db, 1)
        self.relayer.relay_once(db)
        entry = db.get(self.LedgerEntry, 1)
        with self.assertRaises(self.wc.WitnessConflict) as ctx:
            self.wc.submit_voucher(
                {'node': 'unit-node', 'seq': 1,
                 'entry_hash': 'f' * 64, 'prev_hash': entry.prev_hash,
                 'kind': 'entry'})
        self.assertEqual(ctx.exception.code, 'content_conflict')

    def test_relayer_marks_conflict_and_stops_batch(self):
        self.start_witness()
        db = self.session()
        self._seed_entries(db, 3)
        # 模拟见证端对序号 2 判定“同序异内容”：查回执与重发均冲突
        real_get = self.wc.get_receipt
        real_submit = self.wc.submit_voucher

        def fake_get(node, seq):
            if seq == 2:
                raise self.wc.WitnessConflict(409, 'content_conflict', '模拟同序异内容')
            return real_get(node, seq)

        def fake_submit(voucher):
            if voucher.get('seq') == 2:
                raise self.wc.WitnessConflict(409, 'content_conflict', '模拟同序异内容')
            return real_submit(voucher)

        self.wc.get_receipt = fake_get
        self.wc.submit_voucher = fake_submit
        try:
            stats = self.relayer.relay_once(db)
        finally:
            self.wc.get_receipt = real_get
            self.wc.submit_voucher = real_submit
        self.assertEqual(stats['conflict_seq'], 2)
        self.assertEqual(db.get(self.LedgerEntry, 2).witness_state, 'conflict')
        # 本批在冲突处停止，后续条目保持待确认
        self.assertEqual(db.get(self.LedgerEntry, 3).witness_state, 'pending')

    def test_lost_confirmation_is_recovered_via_receipt_lookup(self):
        """凭证已到见证端，但确认响应丢失（本库仍 pending）：靠回执查询接回。"""
        self.start_witness()
        db = self.session()
        self._seed_entries(db, 3)
        self.relayer.relay_once(db)
        # 模拟确认丢失：把第 2 条回退为 pending，但见证端凭证保留
        db.get(self.LedgerEntry, 2).witness_state = 'pending'
        db.get(self.LedgerEntry, 2).witness_receipt = None
        db.commit()
        stats = self.relayer.relay_once(db)
        self.assertEqual(stats['reconnected'], 1)
        self.assertEqual(db.get(self.LedgerEntry, 2).witness_state, 'witnessed')
        self.assertIsNotNone(db.get(self.LedgerEntry, 2).witness_receipt)
        # 见证端没有产生重复凭证
        vouchers = self.wc.fetch_vouchers('unit-node')['vouchers']
        self.assertEqual([v['seq'] for v in vouchers], [0, 1, 2, 3])

    def test_cross_check_detects_local_tamper_and_truncation(self):
        self.start_witness()
        db = self.session()
        self._seed_entries(db, 5)
        self.relayer.relay_once(db)
        from src.services.verification import cross_check
        ok = cross_check(db)
        self.assertTrue(ok['ok'])

        # 场景一：本库某条哈希被改写，见证端独立凭证立刻对不上
        row = db.get(self.LedgerEntry, 3)
        row.entry_hash = 'a' * 64
        db.commit()
        report = cross_check(db)
        self.assertFalse(report['ok'])
        kinds = [e['kind'] for e in report['witness_errors']]
        self.assertIn('witness_mismatch', kinds)

        # 场景二：整段截尾，删掉本库最后两条；见证端链尾更长即可查出
        db.rollback()
        db.delete(db.get(self.LedgerEntry, 5))
        db.delete(db.get(self.LedgerEntry, 4))
        db.commit()
        report2 = cross_check(db)
        self.assertIn('truncation', [e['kind'] for e in report2['witness_errors']])

    def test_fixed_range_verify_while_writes_continue(self):
        """核验固定起止范围，期间新操作照常提交。"""
        self.start_witness()
        db = self.session()
        self._seed_entries(db, 6)
        self.relayer.relay_once(db)
        from src.services.verification import cross_check
        report = cross_check(db, start=1, end=3)
        self.assertEqual(report['range'], {'start': 1, 'end': 3})
        self.assertEqual(report['total'], 3)
        self.assertTrue(report['ok'])
        # 核验期间新写入不受影响
        self.record(db, 'parallel@x', 'job.create', {'id': 100})
        db.commit()
        self.assertEqual(db.get(self.LedgerEntry, 7).seq, 7)
        again = cross_check(db, start=1, end=3)
        self.assertEqual(again['total'], 3)

    def test_verify_when_witness_offline_only_runs_self_check(self):
        db = self.session()
        self._seed_entries(db, 2)
        self.point_witness_to_dead_port()
        from src.services.verification import cross_check
        report = cross_check(db)
        self.assertFalse(report['witness_online'])
        self.assertTrue(report['chain_ok'])
        self.assertIn('witness_offline', [e['kind'] for e in report['witness_errors']])

    def test_legacy_chain_anchor_is_witnessed_but_old_rows_stay_legacy(self):
        import time
        from src.models import Audit
        self.start_witness()
        db = self.session()
        db.add(Audit(actor='old', action='dataset.create', detail={'k': 1},
                     at=time.time() - 50))
        db.commit()
        stats = self.relayer.relay_once(db)  # 内部 ensure_initialized 触发回填
        self.assertFalse(stats['unavailable'])
        # 旧条目保持 legacy；锚点（seq=0 创世）在见证端有 anchor 凭证
        self.assertEqual(db.get(self.LedgerEntry, 1).witness_state, 'legacy')
        remote = self.wc.fetch_vouchers('unit-node')
        self.assertEqual(remote['vouchers'][0]['kind'], 'anchor')
        from src.services.verification import cross_check
        report = cross_check(db)
        # legacy 不参与见证结论，核验仍通过
        self.assertTrue(report['ok'])


if __name__ == '__main__':
    unittest.main()
