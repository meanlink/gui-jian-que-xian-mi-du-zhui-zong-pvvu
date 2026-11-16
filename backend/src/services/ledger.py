"""可核验账本核心：哈希链追加、历史回填、范围核验。

设计要点：
- append_entry 与审计行在同一事务内完成，任一失败由调用方事务整体回滚。
- 并发串行化：PostgreSQL 用 ledger_lock 单行 FOR UPDATE 行级锁，多进程
  安全；SQLite（本地/测试）无行锁，用进程内互斥配合库级写锁，同样不抢号。
  两条路径都保证序号连续、链不分叉、不漏项。
- 历史审计回填为 legacy 状态：只锚定链尾哈希，永不倒填“已见证”结论。
"""
import threading
import time
from sqlalchemy import select
from src.models import Audit, LedgerEntry, LedgerLock, LedgerMeta
from src.services.ledger_crypto import entry_hash, GENESIS_HASH

# 四类受信操作的归类；其余动作归入 system 但同样入链
CATEGORY_MAP = {
    'dataset.create': 'import',
    'job.create': 'trial',
    'job.running': 'trial',
    'job.completed': 'trial',
    'job.failed': 'trial',
    'job.recovered': 'trial',
    'job.approve': 'approval',
    'report.schedule': 'distribution',
    'report.toggle': 'distribution',
    'report.delivered': 'distribution',
}

GENESIS_NOTE = '账本创世锚点：自此哈希链开始，前序哈希固定为全零。'
ANCHOR_META_KEY = 'legacy_anchor_seq'

# SQLite 没有行级锁，用进程内互斥等价实现“追加串行化”
_sqlite_append_lock = threading.RLock()


def category_for(action: str) -> str:
    return CATEGORY_MAP.get(action, 'system')


def _is_postgres(db) -> bool:
    return db.bind.dialect.name == 'postgresql'


class _SerializedAppend:
    """Postgres 用行锁；SQLite 用进程互斥。统一上下文管理器。"""

    def __init__(self, db):
        self.db = db
        self.thread_lock = None if _is_postgres(db) else _sqlite_append_lock

    def __enter__(self):
        if self.thread_lock is not None:
            self.thread_lock.acquire()
            return self
        if self.db.get(LedgerLock, 1) is None:
            self.db.add(LedgerLock(id=1, holder='init'))
            self.db.flush()
        # 行级写锁：并发事务在此排队，保证序号与链严格串行
        self.db.execute(
            select(LedgerLock.id).where(LedgerLock.id == 1).with_for_update())
        return self

    def __exit__(self, *exc):
        if self.thread_lock is not None:
            self.thread_lock.release()
        return False


def _make_entry(db, seq, audit_id, actor, action, category, payload, at,
                prev_hash, state):
    h = entry_hash(seq, audit_id, actor, action, category, payload, at, prev_hash)
    e = LedgerEntry(seq=seq, audit_id=audit_id, actor=actor, action=action,
                    category=category, payload=payload, at=at, prev_hash=prev_hash,
                    entry_hash=h, witness_state=state)
    db.add(e)
    db.flush()
    return e


def _has_legacy_audits(db) -> bool:
    return db.scalar(select(Audit.id).order_by(Audit.id.asc()).limit(1)) is not None


def append_entry(db, audit: Audit) -> LedgerEntry:
    """在当前事务内把审计动作追加进哈希链。失败抛异常，事务回滚。"""
    with _SerializedAppend(db):
        last = db.scalar(
            select(LedgerEntry).order_by(LedgerEntry.seq.desc()).limit(1))
        if last is None:
            # 全新部署：先落创世锚点（待确认，等待见证端确认）
            genesis = _make_entry(db, 0, None, 'system', 'ledger.genesis', 'system',
                                  {'note': GENESIS_NOTE}, time.time(),
                                  GENESIS_HASH, 'pending')
            meta = db.get(LedgerMeta, ANCHOR_META_KEY)
            if meta is None:
                db.add(LedgerMeta(key=ANCHOR_META_KEY,
                                  value={'seq': 0, 'hash': genesis.entry_hash}))
            last = genesis
        seq = last.seq + 1
        return _make_entry(db, seq, audit.id, audit.actor, audit.action,
                           category_for(audit.action), audit.detail or {}, audit.at,
                           last.entry_hash, 'pending')


def backfill_legacy(db) -> int:
    """把账本上线前已存在的审计行回填为 legacy 链，并登记见证锚点序号。

    幂等：已有任何账本行时直接返回。legacy 行不送见证、不标可信。
    """
    with _SerializedAppend(db):
        if db.scalar(select(LedgerEntry.seq).limit(1)) is not None:
            return 0
        now = time.time()
        # 存在旧审计：创世锚点与旧记录一起标 legacy，不倒填可信结论
        genesis = _make_entry(db, 0, None, 'system', 'ledger.genesis', 'system',
                              {'note': GENESIS_NOTE}, now, GENESIS_HASH, 'legacy')
        prev = genesis
        backfilled = 0
        for audit in db.scalars(select(Audit).order_by(Audit.id.asc())):
            prev = _make_entry(db, prev.seq + 1, audit.id, audit.actor, audit.action,
                               category_for(audit.action), audit.detail or {},
                               audit.at, prev.entry_hash, 'legacy')
            backfilled += 1
        meta = db.get(LedgerMeta, ANCHOR_META_KEY)
        anchor_value = {'seq': prev.seq, 'hash': prev.entry_hash}
        if meta is None:
            db.add(LedgerMeta(key=ANCHOR_META_KEY, value=anchor_value))
        else:
            meta.value = anchor_value
        db.flush()
        return backfilled


def ensure_initialized(db):
    """业务侧启动入口：确保历史回填完成（幂等）。"""
    with _SerializedAppend(db):
        if db.scalar(select(LedgerEntry.seq).limit(1)) is None:
            if _has_legacy_audits(db):
                backfill_legacy(db)
        db.flush()


def verify_chain(db, start: int | None = None, end: int | None = None) -> dict:
    """对固定起止范围做只读核验。调用期间其他进程的新写入照常提交。"""
    begin = start if start is not None else 0
    rows = list(db.scalars(
        select(LedgerEntry).where(LedgerEntry.seq >= begin)
        .order_by(LedgerEntry.seq.asc())))
    if end is not None:
        rows = [r for r in rows if r.seq <= end]
    # 子范围核验：先取范围前一条作为链锚点，避免把正常前序哈希误判为断链
    anchor = db.scalar(
        select(LedgerEntry).where(LedgerEntry.seq < begin)
        .order_by(LedgerEntry.seq.desc()).limit(1))
    prev_hash = GENESIS_HASH if anchor is None else anchor.entry_hash
    local_max = db.scalar(
        select(LedgerEntry.seq).order_by(LedgerEntry.seq.desc()).limit(1))
    first_diff = None
    expected_seq = begin
    for row in rows:
        if first_diff is None and row.seq != expected_seq:
            first_diff = {'seq': expected_seq, 'kind': 'gap',
                          'reason': f'序号 {expected_seq} 缺失，链在此断档（可能被删除）',
                          'expected_hash': None, 'actual_hash': None}
        recomputed = entry_hash(row.seq, row.audit_id, row.actor, row.action,
                                row.category, row.payload or {}, row.at, row.prev_hash)
        if first_diff is None and row.prev_hash != prev_hash:
            first_diff = {'seq': row.seq, 'kind': 'link',
                          'reason': '前序哈希不衔接，前一条记录可能被改写',
                          'expected_hash': prev_hash, 'actual_hash': row.prev_hash}
        if first_diff is None and recomputed != row.entry_hash:
            first_diff = {'seq': row.seq, 'kind': 'content',
                          'reason': '内容重算哈希与存证哈希不一致，本行已被改写',
                          'expected_hash': recomputed, 'actual_hash': row.entry_hash}
        prev_hash = row.entry_hash
        expected_seq = row.seq + 1

    counts = {'witnessed': 0, 'pending': 0, 'legacy': 0, 'conflict': 0}
    entries = []
    for row in rows:
        counts[row.witness_state] = counts.get(row.witness_state, 0) + 1
        entries.append({'seq': row.seq, 'audit_id': row.audit_id, 'actor': row.actor,
                        'action': row.action, 'category': row.category, 'at': row.at,
                        'entry_hash': row.entry_hash, 'prev_hash': row.prev_hash,
                        'witness_state': row.witness_state, 'witness_seq': row.witness_seq,
                        'payload': row.payload})
    pending_tail = [r.seq for r in rows if r.witness_state == 'pending']
    return {
        'range': {'start': rows[0].seq if rows else None,
                  'end': rows[-1].seq if rows else None},
        'local_max_seq': local_max,
        'total': len(rows),
        'counts': counts,
        'chain_ok': first_diff is None,
        'first_difference': first_diff,
        'pending_tail': pending_tail,
        'entries': entries,
    }
