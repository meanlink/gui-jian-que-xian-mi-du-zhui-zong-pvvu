import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
REPO_ROOT = os.path.dirname(ROOT)
sys.path.insert(0, REPO_ROOT)


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'ledger.db')
        os.environ['DATABASE_URL'] = 'sqlite:///' + self.db_path
        os.environ['WITNESS_SECRET'] = 'unit-test-secret'
        os.environ['WITNESS_NODE'] = 'unit-node'
        os.environ['WITNESS_TIMEOUT'] = '2'
        os.environ['ARTIFACT_DIR'] = os.path.join(self.tmp.name, 'artifacts')
        # 延迟导入，确保环境变量先生效
        from src.config import database as db_mod
        from sqlalchemy import create_engine, event
        engine = create_engine(
            'sqlite:///' + self.db_path,
            connect_args={'check_same_thread': False, 'timeout': 30})

        @event.listens_for(engine, 'connect')
        def _setup(sqlite_conn, _record):
            sqlite_conn.execute('PRAGMA journal_mode=WAL')
            sqlite_conn.execute('PRAGMA busy_timeout=30000')
        db_mod.engine = engine
        db_mod.Session.configure(bind=engine)
        from src.config import settings
        settings.WITNESS_SECRET = 'unit-test-secret'
        settings.WITNESS_NODE = 'unit-node'
        settings.WITNESS_TIMEOUT = 2.0
        settings.ARTIFACT_DIR = os.environ['ARTIFACT_DIR']
        self._settings = settings
        from src.models import Audit, LedgerEntry
        self.Audit = Audit
        self.LedgerEntry = LedgerEntry
        import src.services.ledger_crypto as crypto
        import src.services.witness_client as wc
        import src.services.relayer as relayer
        self.crypto = crypto
        self.wc = wc
        self.relayer = relayer
        from src.config.database import Base
        Base.metadata.create_all(engine)

    def tearDown(self):
        from src.config import database as db_mod
        db_mod.engine.dispose()
        self.tmp.cleanup()

    def session(self):
        from src.config.database import Session
        return Session()

    def record(self, db, actor, action, detail):
        from src.services.audit import record
        return record(db, actor, action, detail)

    def start_witness(self):
        """启动进程内见证端，返回 HTTP server。"""
        import importlib
        import witness.server as ws
        importlib.reload(ws)
        ws.DB_PATH = os.path.join(self.tmp.name, 'witness.db')
        ws.SECRET = 'unit-test-secret'
        ws.init_db()
        self._ws = ws
        server = ThreadingHTTPServer(('127.0.0.1', 0), ws.Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._settings.WITNESS_URL = f'http://127.0.0.1:{port}'
        self.wc.WITNESS_URL = f'http://127.0.0.1:{port}'
        self.server = server
        return server

    def point_witness_to_dead_port(self):
        dead = 'http://127.0.0.1:9'  # 拒接端口，快速失败
        self._settings.WITNESS_URL = dead
        self.wc.WITNESS_URL = dead
