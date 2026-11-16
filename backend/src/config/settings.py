import os
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./local.db')
REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')
JWT_SECRET = os.environ.get('JWT_SECRET', 'local-development-change-before-deploy')
ARTIFACT_DIR = os.getenv('ARTIFACT_DIR', '/artifacts')
# 独立见证端：断线只让凭证停在 pending，不阻塞业务
WITNESS_URL = os.getenv('WITNESS_URL', 'http://witness:9100')
# 本库与见证端共享的 HMAC 密钥（生产应通过机密管理分别注入两端）
WITNESS_SECRET = os.getenv('WITNESS_SECRET', 'rail-density-witness-local-demo-secret')
WITNESS_TIMEOUT = float(os.getenv('WITNESS_TIMEOUT', '3'))
WITNESS_NODE = os.getenv('WITNESS_NODE', 'rail-density-ledger')
