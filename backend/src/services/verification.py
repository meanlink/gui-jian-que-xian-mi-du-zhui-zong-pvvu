"""范围核验：本库哈希链自检 + 见证端独立凭证交叉比对。"""
from sqlalchemy import select
from src.models import LedgerEntry
from src.config.settings import WITNESS_NODE
from src.services import witness_client
from src.services.witness_client import WitnessUnavailable, WitnessConflict
from src.services.ledger import verify_chain

# 这些是状态提示而非账本被破坏的实质差异，不影响核验通过结论，也不占“首处差异”
BENIGN_KINDS = ('receipt_pending', 'witness_offline')


def cross_check(db, start: int | None = None, end: int | None = None) -> dict:
    report = verify_chain(db, start, end)

    local = {e['seq']: e for e in report['entries']}
    witness_online, witness_tip, witness_errors = False, None, []
    try:
        remote = witness_client.fetch_vouchers(WITNESS_NODE, report['range']['start'],
                                               report['range']['end'])
        witness_online = True
        witness_tip = remote['tip']
        remote_map = {v['seq']: v for v in remote['vouchers']}

        for seq, entry in local.items():
            if entry['witness_state'] in ('legacy',):
                continue  # 旧记录只看链内自洽，不就见证结论表态
            voucher = remote_map.get(seq)
            if voucher is None:
                # 仅当该序号落在本次向见证端查询的范围内且本库称已见证，
                # 才能判定为独立凭证缺失；范围之外未查询，不做结论
                in_query = (report['range']['start'] is None
                            or seq >= report['range']['start']) and \
                           (report['range']['end'] is None
                            or seq <= report['range']['end'])
                if entry['witness_state'] == 'witnessed' and in_query:
                    witness_errors.append({
                        'seq': seq, 'kind': 'witness_missing',
                        'reason': '本库标记已见证，但见证端独立存储中查无凭证'})
                # pending：尚未送达或未确认尾段，保持待确认
                continue
            if voucher['entry_hash'] != entry['entry_hash']:
                witness_errors.append({
                    'seq': seq, 'kind': 'witness_mismatch',
                    'reason': '本库条目哈希与见证端独立凭证不一致，本库记录已被改写',
                    'local_hash': entry['entry_hash'],
                    'witness_hash': voucher['entry_hash']})
            elif entry['witness_state'] != 'witnessed':
                # 独立端已有凭证而本库未标：把接回机会留给中继，核验先如实报告
                witness_errors.append({
                    'seq': seq, 'kind': 'receipt_pending',
                    'reason': '见证端已存凭证，本库确认状态尚未接回'})

        # 整段截尾检测：本库链尾短于见证端已见证序号
        if witness_tip is not None and report['local_max_seq'] < witness_tip['seq']:
            witness_errors.append({
                'seq': report['local_max_seq'] + 1, 'kind': 'truncation',
                'reason': f"见证端已见证至序号 {witness_tip['seq']}，"
                          f"本库仅止于 {report['local_max_seq']}，账本被整段截尾"})
    except WitnessUnavailable as exc:
        witness_errors.append({'seq': None, 'kind': 'witness_offline',
                               'reason': f'见证端暂不可达，仅完成本库链内自检：{exc}'})
    except WitnessConflict as exc:
        witness_errors.append({'seq': None, 'kind': 'witness_rejected',
                               'reason': f'见证端拒绝查询：{exc.code} {exc.detail}'})

    first = report['first_difference']
    if first is None:
        first = next((e for e in witness_errors if e['kind'] not in BENIGN_KINDS), None)
    report['witness_online'] = witness_online
    report['witness_tip'] = witness_tip
    report['witness_errors'] = witness_errors
    report['ok'] = report['chain_ok'] and not [
        e for e in witness_errors if e['kind'] not in BENIGN_KINDS]
    return report


def pending_backlog(db) -> dict:
    rows = list(db.scalars(
        select(LedgerEntry).where(LedgerEntry.witness_state == 'pending')
        .order_by(LedgerEntry.seq.asc())))
    conflict = list(db.scalars(
        select(LedgerEntry).where(LedgerEntry.witness_state == 'conflict')
        .order_by(LedgerEntry.seq.asc())))
    return {
        'pending_count': len(rows),
        'pending': [{'seq': r.seq, 'action': r.action, 'category': r.category,
                     'actor': r.actor, 'at': r.at, 'entry_hash': r.entry_hash}
                    for r in rows],
        'conflict_count': len(conflict),
        'conflicts': [{'seq': r.seq, 'action': r.action,
                       'reason': r.witness_receipt or '', 'entry_hash': r.entry_hash}
                      for r in conflict],
    }
