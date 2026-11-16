import hashlib
import hmac
import json
from typing import Any

GENESIS_HASH = '0' * 64


def canonical_json(value: Any) -> bytes:
    """确定性 JSON：键排序、无空白、原样保留中文，保证两端逐字节一致。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def entry_hash(seq: int, audit_id: int | None, actor: str, action: str,
               category: str, payload: dict, at: float, prev_hash: str) -> str:
    body = canonical_json({
        'seq': seq,
        'audit_id': audit_id,
        'actor': actor,
        'action': action,
        'category': category,
        'payload': payload,
        'at': at,
        'prev_hash': prev_hash,
    })
    return hashlib.sha256(body).hexdigest()


def hmac_sign(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()
