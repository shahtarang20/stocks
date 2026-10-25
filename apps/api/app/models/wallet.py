"""An on-chain wallet a user has proved they control."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class LinkedWallet(Base):
    """One wallet per account per chain, proven by a signature over a server-issued message.

    Linking is what lets the platform say "this address belongs to this account", which
    is the prerequisite for ever comparing a user's on-chain tokens with the ledger.
    An address can belong to only one account on a chain, so two accounts can never
    claim the same tokens.
    """

    __tablename__ = "linked_wallets"
    __table_args__ = (
        UniqueConstraint("chain_id", "address", name="uq_linked_wallets_chain_address"),
        UniqueConstraint("user_id", "chain_id", name="uq_linked_wallets_user_chain"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Stored lower-cased; displayed checksummed.
    address: Mapped[str] = mapped_column(String(42), nullable=False)
    linked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
