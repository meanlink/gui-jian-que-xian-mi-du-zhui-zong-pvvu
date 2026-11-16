"""见证端 HTTP 客户端：HMAC 签名、短超时、断线只影响凭证状态不影响业务。"""
import json
import urllib.error
import urllib.request
from src.config.settings import WITNESS_URL, WITNESS_SECRET, WITNESS_TIMEOUT
from src.services.ledger_crypto import hmac_sign


class WitnessUnavailable(Exception):
    """见证端断线/超时/网络错误。"""


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
        headers['X-Witness-Sign'] = hmac_sign(WITNESS_SECRET, data)
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(WITNESS_URL + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=WITNESS_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode('utf-8'))
        except Exception:
            detail = {'code': 'witness_error', 'detail': '见证端返回异常'}
        # 业务语义错误（冲突、跳号、缺锚）原样上抛，不按断线处理
        raise WitnessConflict(exc.code, detail.get('code', 'witness_error'),
                              detail.get('detail', ''))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WitnessUnavailable(str(exc)) from exc


class WitnessConflict(Exception):
    def __init__(self, status, code, detail):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def submit_voucher(voucher: dict) -> dict:
    return _request('POST', '/vouchers', voucher)


def get_receipt(node: str, seq: int) -> dict:
    return _request('POST', '/vouchers/receipt', {'node': node, 'seq': seq})


def fetch_vouchers(node: str, start: int | None = None, end: int | None = None) -> dict:
    q = '/vouchers?node=' + node
    if start is not None:
        q += '&start=' + str(start)
    if end is not None:
        q += '&end=' + str(end)
    return _request('GET', q)
