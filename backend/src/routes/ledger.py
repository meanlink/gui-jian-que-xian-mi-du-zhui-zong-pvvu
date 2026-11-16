from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from src.config.database import get_db
from src.models import LedgerEntry
from src.middlewares.auth import current_user, admin_only
from src.services.verification import cross_check, pending_backlog
from src.services.relayer import relay_once

router = APIRouter(prefix='/ledger')


@router.get('/entries')
def entries(limit: int = Query(200, le=1000), offset: int = 0,
            db=Depends(get_db), user=Depends(current_user)):
    total = db.scalar(select(func.count()).select_from(LedgerEntry))
    rows = db.scalars(select(LedgerEntry).order_by(LedgerEntry.seq.desc())
                      .limit(limit).offset(offset)).all()
    return {'total': total, 'items': [{
        'seq': r.seq, 'audit_id': r.audit_id, 'actor': r.actor, 'action': r.action,
        'category': r.category, 'payload': r.payload, 'at': r.at,
        'prev_hash': r.prev_hash, 'entry_hash': r.entry_hash,
        'witness_state': r.witness_state, 'witness_seq': r.witness_seq,
        'witness_receipt': r.witness_receipt,
    } for r in rows]}


@router.get('/verify')
def verify(start: int | None = None, end: int | None = None,
           db=Depends(get_db), user=Depends(current_user)):
    if start is not None and end is not None and start > end:
        raise HTTPException(422, '起始序号不能大于终止序号')
    if start is not None and start < 0:
        raise HTTPException(422, '起始序号不能小于 0')
    return cross_check(db, start, end)


@router.get('/pending')
def pending(db=Depends(get_db), user=Depends(current_user)):
    return pending_backlog(db)


@router.post('/relay', status_code=200)
def trigger_relay(db=Depends(get_db), user=Depends(admin_only)):
    # 仅手动触发一轮补发；见证端断线时同样快速返回，不抛 5xx
    return relay_once(db)
