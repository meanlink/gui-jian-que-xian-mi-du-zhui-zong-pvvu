"""账本 → 见证端的补发中继。

关键保证：
- 按账本原序逐条补送，跳号会被见证端拒绝，故绝不乱序。
- 所有网络异常都被吞掉并返回未送条数：见证端断线不阻塞任何业务请求。
- 同一条凭证重复 POST 时见证端幂等返回原回执，不新增凭证。
- 已送达但确认响应丢失（本库仍为 pending）：先查回执再接回，无需重发。
- 同序异内容 / 链分叉被见证端拒绝时，本库标 conflict 并停止本批，等待核验介入。
"""
import logging
import time
from sqlalchemy import select, func
from src.models import LedgerEntry, LedgerMeta
from src.config.settings import WITNESS_NODE
from src.services.ledger import ensure_initialized, ANCHOR_META_KEY
from src.services import witness_client
from src.services.witness_client import WitnessUnavailable, WitnessConflict

BATCH_LIMIT = 100


def _apply_receipt(entry, receipt):
    entry.witness_receipt = receipt['receipt']
    entry.witness_seq = receipt['witness_seq']
    entry.witness_at = receipt.get('at', time.time())


def relay_once(db) -> dict:
    """执行一轮补发。返回统计字典；任何见证端故障都不抛出。"""
    ensure_initialized(db)
    sent, conflict_seq, unavailable, reconnected = 0, None, False, 0
    anchor = db.get(LedgerMeta, ANCHOR_META_KEY)

    try:
        # 1) 先确保见证端存在该节点的锚点（历史链尾或创世锚点）
        if anchor is None:
            genesis = db.get(LedgerEntry, 0)
            if genesis is None:
                return {'sent': 0, 'pending': 0, 'unavailable': True}
            anchor = LedgerMeta(key=ANCHOR_META_KEY,
                                value={'seq': 0, 'hash': genesis.entry_hash})
            db.add(anchor)
            db.flush()
        anchor_entry = db.get(LedgerEntry, anchor.value['seq'])
        try:
            resp = witness_client.submit_voucher(
                {'node': WITNESS_NODE, 'seq': anchor.value['seq'],
                 'entry_hash': anchor.value['hash'],
                 'prev_hash': anchor_entry.prev_hash, 'kind': 'anchor'})
        except WitnessUnavailable:
            return {'sent': 0, 'pending': _pending_count(db), 'unavailable': True}
        _apply_receipt(anchor_entry, resp)
        # 创世锚点在全新部署里属待确认尾段；历史锚点保持 legacy 不倒填结论
        if anchor_entry.witness_state == 'pending':
            anchor_entry.witness_state = 'witnessed'
            if not resp.get('deduplicated'):
                sent += 1
        db.commit()

        # 2) 按原序处理所有 pending（含确认丢失后重连的条目）
        pending = list(db.scalars(
            select(LedgerEntry).where(LedgerEntry.witness_state == 'pending')
            .order_by(LedgerEntry.seq.asc()).limit(BATCH_LIMIT)))
        for entry in pending:
            try:
                # 先查回执：确认响应丢失时可直接接回，不制造重复凭证
                recovered = False
                try:
                    receipt = witness_client.get_receipt(WITNESS_NODE, entry.seq)
                    recovered = True  # 独立端已存凭证而本库仍 pending：确认丢失后接回
                except WitnessConflict as exc:
                    if exc.code != 'not_witnessed':
                        raise
                    receipt = witness_client.submit_voucher(
                        {'node': WITNESS_NODE, 'seq': entry.seq,
                         'entry_hash': entry.entry_hash, 'prev_hash': entry.prev_hash,
                         'kind': 'entry'})
                _apply_receipt(entry, receipt)
                entry.witness_state = 'witnessed'
                db.commit()
                sent += 1
                if recovered or receipt.get('deduplicated'):
                    reconnected += 1
            except WitnessUnavailable:
                db.rollback()
                unavailable = True
                break
            except WitnessConflict as exc:
                logging.exception('Witness rejected voucher at seq=%s: %s',
                                  entry.seq, exc.code)
                db.rollback()
                fresh = db.get(LedgerEntry, entry.seq)
                if fresh is not None and fresh.witness_state == 'pending':
                    fresh.witness_state = 'conflict'
                    fresh.witness_receipt = f'{exc.code}: {exc.detail}'
                    db.commit()
                conflict_seq = entry.seq
                break
    except WitnessUnavailable:
        db.rollback()
        unavailable = True
    except WitnessConflict as exc:
        logging.exception('Anchor rejected by witness: %s', exc.code)
        db.rollback()
        conflict_seq = anchor.value['seq']

    return {'sent': sent, 'reconnected': reconnected,
            'pending': _pending_count(db), 'unavailable': unavailable,
            'conflict_seq': conflict_seq}


def _pending_count(db) -> int:
    return db.scalar(select(func.count()).select_from(LedgerEntry)
                     .where(LedgerEntry.witness_state == 'pending')) or 0
