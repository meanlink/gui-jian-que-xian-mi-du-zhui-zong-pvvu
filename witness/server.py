"""独立审计见证端（与业务库完全分离的存储与进程）。

职责：
- 接收业务账本逐条送来的凭证（entry_hash + 链上前序哈希），独立另存。
- 见证端自身再维护一条凭证哈希链，回执 receipt 随链生成。
- 同序号重复送达且内容一致：幂等返回原回执，不新增凭证。
- 同序号内容不一致、或前序哈希与见证端链尾不符：拒绝（409），不入链。
- 缺序号（跳号）：拒绝（422），迫使对端按原序补送。
- 历史锚点 kind=anchor 用于为“账本上线前的旧链”建立起点，仅锚定哈希，
  不产生任何对旧内容的可信背书。

仅依赖标准库，可独立部署在与业务系统不同的主机/容器。
"""
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.getenv('WITNESS_DB', '/data/witness.db')
SECRET = os.getenv('WITNESS_SECRET', 'rail-density-witness-local-demo-secret')
LISTEN_HOST = os.getenv('WITNESS_HOST', '0.0.0.0')
LISTEN_PORT = int(os.getenv('WITNESS_PORT', '9100'))
ZERO_HASH = '0' * 64

_write_lock = threading.Lock()


def _connect():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=8000')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS vouchers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node TEXT NOT NULL,
            seq BIGINT NOT NULL,
            entry_hash TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            kind TEXT NOT NULL,
            at REAL NOT NULL,
            receipt TEXT NOT NULL,
            UNIQUE(node, seq)
        );
        CREATE TABLE IF NOT EXISTS chains(
            node TEXT PRIMARY KEY,
            tip_seq BIGINT NOT NULL,
            tip_hash TEXT NOT NULL,
            tip_receipt TEXT NOT NULL
        );
        ''')


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _receipt(node, seq, entry_hash, prev_hash, kind, prev_receipt) -> str:
    return hashlib.sha256(_canonical({
        'node': node, 'seq': seq, 'entry_hash': entry_hash,
        'prev_hash': prev_hash, 'kind': kind, 'prev_receipt': prev_receipt,
    })).hexdigest()


class WitnessError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def submit_voucher(payload: dict) -> dict:
    node = payload.get('node')
    seq = payload.get('seq')
    entry_hash = payload.get('entry_hash')
    prev_hash = payload.get('prev_hash')
    kind = payload.get('kind', 'entry')
    if not node or not isinstance(seq, int) or seq < 0 \
            or not isinstance(entry_hash, str) or len(entry_hash) != 64 \
            or not isinstance(prev_hash, str) or len(prev_hash) != 64 \
            or kind not in ('entry', 'anchor'):
        raise WitnessError(422, 'bad_voucher', '凭证字段不完整或格式非法')
    with _write_lock, _connect() as conn:
        existing = conn.execute(
            'SELECT * FROM vouchers WHERE node=? AND seq=?', (node, seq)).fetchone()
        if existing is not None:
            # 重试不新增凭证；同序异内容拒绝
            if existing['entry_hash'] == entry_hash and existing['prev_hash'] == prev_hash \
                    and existing['kind'] == kind:
                return {'deduplicated': True, 'seq': seq, 'node': node,
                        'witness_seq': existing['id'], 'receipt': existing['receipt'],
                        'at': existing['at']}
            raise WitnessError(409, 'content_conflict',
                               f'节点 {node} 序号 {seq} 已存在不同内容的凭证，拒绝改写')
        chain = conn.execute('SELECT * FROM chains WHERE node=?', (node,)).fetchone()
        if chain is None:
            if kind != 'anchor':
                raise WitnessError(422, 'missing_anchor',
                                   '该节点尚未建立历史锚点，不能直接见证业务凭证')
            expected_seq, prev_receipt = 0, ZERO_HASH
        else:
            expected_seq = chain['tip_seq'] + 1
            prev_receipt = chain['tip_receipt']
            if seq != expected_seq:
                raise WitnessError(422, 'sequence_gap',
                                   f'期望序号 {expected_seq}，收到 {seq}，请按原序补送')
            if kind == 'entry' and prev_hash != chain['tip_hash']:
                raise WitnessError(409, 'chain_fork',
                                   '前序哈希与见证端链尾不一致，疑似账本分叉或截尾后重写')
        now = time.time()
        receipt = _receipt(node, seq, entry_hash, prev_hash, kind, prev_receipt)
        cur = conn.execute(
            'INSERT INTO vouchers(node,seq,entry_hash,prev_hash,kind,at,receipt)'
            ' VALUES(?,?,?,?,?,?,?)',
            (node, seq, entry_hash, prev_hash, kind, now, receipt))
        conn.execute(
            'INSERT INTO chains(node,tip_seq,tip_hash,tip_receipt) VALUES(?,?,?,?) '
            'ON CONFLICT(node) DO UPDATE SET tip_seq=excluded.tip_seq,'
            ' tip_hash=excluded.tip_hash, tip_receipt=excluded.tip_receipt',
            (node, seq, entry_hash, receipt))
        return {'deduplicated': False, 'seq': seq, 'node': node,
                'witness_seq': cur.lastrowid, 'receipt': receipt, 'at': now}


def get_receipt(node: str, seq: int) -> dict:
    with _connect() as conn:
        row = conn.execute(
            'SELECT id,receipt,at,entry_hash FROM vouchers WHERE node=? AND seq=?',
            (node, seq)).fetchone()
    if row is None:
        raise WitnessError(404, 'not_witnessed', f'序号 {seq} 暂无见证凭证')
    return {'node': node, 'seq': seq, 'witness_seq': row['id'],
            'receipt': row['receipt'], 'at': row['at'], 'entry_hash': row['entry_hash']}


def list_vouchers(node: str, start: int | None, end: int | None) -> dict:
    sql = 'SELECT seq,entry_hash,prev_hash,kind,at,receipt,id FROM vouchers WHERE node=?'
    args = [node]
    if start is not None:
        sql += ' AND seq>=?'
        args.append(start)
    if end is not None:
        sql += ' AND seq<=?'
        args.append(end)
    sql += ' ORDER BY seq ASC'
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
        chain = conn.execute('SELECT * FROM chains WHERE node=?', (node,)).fetchone()
    return {'node': node, 'vouchers': [dict(r) for r in rows],
            'tip': None if chain is None else {'seq': chain['tip_seq'],
                                               'hash': chain['tip_hash'],
                                               'receipt': chain['tip_receipt']}}


class Handler(BaseHTTPRequestHandler):
    server_version = 'LedgerWitness/1.0'

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length)
        signature = self.headers.get('X-Witness-Sign', '')
        expected = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise WitnessError(401, 'bad_signature', '见证密钥签名校验失败')
        try:
            return json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise WitnessError(422, 'bad_json', '请求体不是合法 JSON')

    def do_GET(self):
        path = self.path.split('?', 1)
        params = {}
        if len(path) == 2:
            from urllib.parse import parse_qs
            params = {k: v[0] for k, v in parse_qs(path[1]).items()}
        try:
            if path[0] == '/health':
                self._send(200, {'status': 'ok', 'service': 'ledger-witness'})
            elif path[0] == '/vouchers':
                if not params.get('node'):
                    raise WitnessError(422, 'bad_request', '缺少 node 参数')
                start = int(params['start']) if 'start' in params else None
                end = int(params['end']) if 'end' in params else None
                self._send(200, list_vouchers(params['node'], start, end))
            else:
                self._send(404, {'detail': 'not found'})
        except WitnessError as e:
            self._send(e.status, {'code': e.code, 'detail': e.message})
        except (ValueError, TypeError):
            self._send(422, {'code': 'bad_request', 'detail': '查询参数非法'})

    def do_POST(self):
        try:
            payload = self._read_json()
            if self.path == '/vouchers':
                self._send(200, submit_voucher(payload))
            elif self.path == '/vouchers/receipt':
                self._send(200, get_receipt(payload['node'], int(payload['seq'])))
            else:
                self._send(404, {'detail': 'not found'})
        except WitnessError as e:
            self._send(e.status, {'code': e.code, 'detail': e.message})
        except KeyError:
            self._send(422, {'code': 'bad_request', 'detail': '缺少 node 或 seq'})
        except (ValueError, TypeError):
            self._send(422, {'code': 'bad_request', 'detail': '参数类型非法'})


def main():
    init_db()
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f'witness listening on {LISTEN_HOST}:{LISTEN_PORT} db={DB_PATH}', flush=True)
    httpd.serve_forever()


if __name__ == '__main__':
    main()
