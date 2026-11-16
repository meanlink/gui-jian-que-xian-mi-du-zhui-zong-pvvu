import time
from sqlalchemy.exc import OperationalError, IntegrityError
from src.models import Audit
from src.services.ledger import append_entry, _is_postgres

# SQLite（本地/测试）无行级锁，多进程竞争时用有界重试兜底；
# PostgreSQL 由 ledger_lock 行锁串行化，正常不会进入重试。
_MAX_RETRY = 20


def record(db, actor, action, detail=None):
    """写审计并把同一动作追加进可核验账本。

    两步在同一事务内：任一步失败均抛出，调用方 commit 不会发生，
    即“操作与审计一并保存，任一失败撤销本次变更”。

    SQLite 跨进程可能遇到写锁占用或序号竞争，用 SAVEPOINT 包住整笔
    （审计 + 账本）做有界重试，保证要么整笔成功、要么完全不留痕。
    """
    if _is_postgres(db):
        return _append_atomically(db, actor, action, detail)

    last_exc = None
    for attempt in range(_MAX_RETRY):
        try:
            with db.begin_nested():  # SAVEPOINT：失败可安全重试，不污染外层事务
                return _append_atomically(db, actor, action, detail)
        except (OperationalError, IntegrityError) as exc:
            last_exc = exc
            time.sleep(0.02 * (attempt + 1))
    raise last_exc


def _append_atomically(db, actor, action, detail):
    audit = Audit(actor=actor, action=action, detail=detail or {}, at=time.time())
    db.add(audit)
    db.flush()  # 拿到 audit.id；失败则整条事务回滚
    append_entry(db, audit)  # 哈希链追加失败同样回滚
    return audit
