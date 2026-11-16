from sqlalchemy import String, Double, BigInteger, JSON, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base

# 见证状态：
# pending   尚未被见证端确认（含断线积压与最新尾段）
# witnessed 见证端已存独立凭证
# conflict  同序异内容，被见证端拒绝（凭证与本库内容不一致）
# legacy    账本上线前的旧记录，标记为未见证，永不倒填可信结论


class LedgerLock(Base):
    """单行表：序列化账本追加，保证多进程不抢同一序号、不分叉。"""
    __tablename__ = 'ledger_lock'
    id: Mapped[int] = mapped_column(primary_key=True)
    holder: Mapped[str] = mapped_column(String(120), default='')


class LedgerMeta(Base):
    """账本内部元数据，例如历史回填锚点序号。"""
    __tablename__ = 'ledger_meta'
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class LedgerEntry(Base):
    __tablename__ = 'ledger_entries'
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # 与 audits 表一一对应；genesis 与 legacy 回填行为空
    audit_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(150))
    action: Mapped[str] = mapped_column(String(80))
    # 账本事件类别：import / trial / approval / distribution / system
    category: Mapped[str] = mapped_column(String(30), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[float] = mapped_column(Double)
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True)
    witness_state: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    witness_receipt: Mapped[str | None] = mapped_column(Text, nullable=True)
    witness_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    witness_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
