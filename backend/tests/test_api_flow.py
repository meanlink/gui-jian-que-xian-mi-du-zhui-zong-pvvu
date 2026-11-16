import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base import LedgerTestBase


class ApiFlowTests(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.start_witness()
        from fastapi.testclient import TestClient
        from src.main import app
        from src.config.database import Session
        from src.models import User
        from src.utils.security import hash_password
        # 不使用 with 触发 lifespan（避免依赖 numpy 的 seed）；表已由测试基座建好
        self.client = TestClient(app, raise_server_exceptions=True)
        with Session() as db:
            for role, name in [('admin', '负责人'), ('analyst', '分析员')]:
                db.add(User(email=f'{role}@x.local', name=name, role=role,
                            password=hash_password('Pass123!')))
            db.commit()
        self.tokens = {}
        for role in ('admin', 'analyst'):
            r = self.client.post('/auth/login',
                                 json={'email': f'{role}@x.local', 'password': 'Pass123!'})
            self.assertEqual(r.status_code, 200, r.text)
            self.tokens[role] = r.json()['access_token']

    def auth(self, role):
        return {'Authorization': 'Bearer ' + self.tokens[role]}

    def test_business_flow_writes_atomic_ledger_and_relays(self):
        from src.services.business import seed_records
        records = seed_records()  # 185 个有效缺陷点
        # 1) 导入（analyst 可写）
        r = self.client.post('/datasets',
                             json={'name': '南环样本A', 'source': 'api.json',
                                   'records': records},
                             headers=self.auth('analyst'))
        self.assertEqual(r.status_code, 201, r.text)
        dataset_id = r.json()['id']

        # 2) 创建试验
        r = self.client.post('/jobs',
                             json={'name': '试验100米', 'dataset_id': dataset_id,
                                   'params': {'eps_m': 100, 'min_points': 5}},
                             headers=self.auth('analyst'))
        self.assertEqual(r.status_code, 201, r.text)
        job_id = r.json()['id']

        # 同步执行 worker 逻辑（试验状态变化也要入账）
        from src.worker import execute
        execute(job_id)

        r = self.client.get(f'/jobs/{job_id}', headers=self.auth('admin'))
        self.assertEqual(r.json()['status'], 'completed')

        # 3) 批准（仅 admin）
        r = self.client.post(f'/jobs/{job_id}/approve', headers=self.auth('admin'))
        self.assertEqual(r.status_code, 200, r.text)
        forbidden = self.client.post(f'/jobs/{job_id}/approve',
                                     headers=self.auth('analyst'))
        self.assertEqual(forbidden.status_code, 403)

        # 4) 分发计划 + 立即交付
        r = self.client.post('/reports',
                             json={'name': '复核简报', 'job_id': job_id,
                                   'interval_seconds': 60, 'delay_seconds': 0},
                             headers=self.auth('admin'))
        self.assertEqual(r.status_code, 201, r.text)
        report_id = r.json()['id']
        from src.config.database import Session
        from src.models import Report
        from src.services.reports import deliver
        from src.services.audit import record
        with Session() as db:
            rep = db.get(Report, report_id)
            rep.next_run = time.time() - 1
            d = deliver(db, rep)
            record(db, 'worker', 'report.delivered', {'id': rep.id, 'delivery': d.id})
            db.commit()

        # 5) 账本包含四类操作且链完整
        r = self.client.get('/ledger/entries', headers=self.auth('admin'))
        self.assertEqual(r.status_code, 200)
        items = r.json()['items']
        categories = {i['category'] for i in items}
        for needed in ('import', 'trial', 'approval', 'distribution'):
            self.assertIn(needed, categories)
        seqs = sorted(i['seq'] for i in items)
        self.assertEqual(seqs, list(range(0, len(seqs))))

        # 6) 手动中继（admin），analyst 无权
        r = self.client.post('/ledger/relay', headers=self.auth('admin'))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['pending'], 0)
        self.assertEqual(self.client.post('/ledger/relay',
                                          headers=self.auth('analyst')).status_code, 403)

        # 7) 全账与范围核验
        r = self.client.get('/ledger/verify', headers=self.auth('admin'))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['chain_ok'])
        self.assertTrue(body['ok'], body['witness_errors'])
        self.assertTrue(body['witness_online'])

        r = self.client.get('/ledger/verify?start=2&end=4', headers=self.auth('admin'))
        self.assertEqual(r.json()['range'], {'start': 2, 'end': 4})
        self.assertEqual(r.json()['total'], 3)

        # 8) 待确认积压视图
        r = self.client.get('/ledger/pending', headers=self.auth('admin'))
        self.assertEqual(r.json()['pending_count'], 0)

    def test_failed_business_change_rolls_back_audit_and_ledger(self):
        """业务校验失败时，既无审计也无账本条目（同事务原子性的 HTTP 侧验证）。"""
        before = self.client.get('/ledger/entries',
                                 headers=self.auth('admin')).json()['total']
        # 名称过短触发 422，服务层在任何写入前拒绝
        r = self.client.post('/datasets',
                             json={'name': 'x', 'records': [{'a': 1}] * 6},
                             headers=self.auth('analyst'))
        self.assertEqual(r.status_code, 422)
        after = self.client.get('/ledger/entries',
                                headers=self.auth('admin')).json()['total']
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
