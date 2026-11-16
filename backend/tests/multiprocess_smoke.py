"""跨进程并发记账冒烟脚本（不依赖测试框架）。

启动 N 个独立 OS 进程，各自打开同一个 SQLite 库并发写入审计+账本，
最后在主进程核验：序号连续无缺、哈希链完整、无分叉、条目数正确。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def worker(db_path, worker_id, count):
    os.environ['DATABASE_URL'] = 'sqlite:///' + db_path
    os.environ['ARTIFACT_DIR'] = tempfile.mkdtemp()
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from src.models import Audit  # noqa: F401 确保模型注册
    from src.models.ledger import LedgerEntry  # noqa: F401
    from src.services.audit import record
    engine = create_engine(
        'sqlite:///' + db_path,
        connect_args={'check_same_thread': False, 'timeout': 60})

    @event.listens_for(engine, 'connect')
    def _pragma(conn, _):
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=60000')

    # 表由主进程统一创建，子进程不做并发 DDL
    Session = sessionmaker(engine)
    for i in range(count):
        s = Session()
        try:
            record(s, f'proc{worker_id}@x', 'dataset.create',
                   {'worker': worker_id, 'i': i})
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


def main():
    import multiprocessing as mp
    tmp = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmp.name, 'concurrent.db')
    # 主进程先建表，避免子进程并发 DDL
    os.environ['DATABASE_URL'] = 'sqlite:///' + db_path
    from sqlalchemy import create_engine as _create
    from src.config.database import Base as _Base
    from src.models.ledger import LedgerEntry as _L  # noqa: F401
    _Base.metadata.create_all(_create('sqlite:///' + db_path))

    procs_n, per = 6, 20
    ctx = mp.get_context('spawn')
    procs = [ctx.Process(target=worker, args=(db_path, w, per))
             for w in range(procs_n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(300)
    assert all(p.exitcode == 0 for p in procs), '存在进程异常退出'

    os.environ['DATABASE_URL'] = 'sqlite:///' + db_path
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.config.database import Base
    from src.models.ledger import LedgerEntry  # noqa: F401
    from src.services.ledger import verify_chain
    engine = create_engine('sqlite:///' + db_path)
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    with Session() as db:
        report = verify_chain(db)
        seqs = [e['seq'] for e in report['entries']]
        expected = list(range(len(seqs)))
        assert report['chain_ok'], '哈希链不完整'
        assert seqs == expected, f'序号分叉或缺号: {seqs[:5]}...'
        hashes = [e['entry_hash'] for e in report['entries']]
        assert len(hashes) == len(set(hashes)), '存在重复哈希（分叉）'
        # 1 个创世锚点 + procs_n*per 条业务凭证
        assert len(seqs) == 1 + procs_n * per, len(seqs)
        print(f'MULTI-PROCESS OK: {procs_n} 进程 x {per} 条 = '
              f'{procs_n * per} 业务条目，链序号 0..{seqs[-1]} 连续无分叉')
    tmp.cleanup()


if __name__ == '__main__':
    main()
