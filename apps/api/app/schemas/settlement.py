"""Pydantic schemas for Phase 10 on-chain settlement & smart contracts."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContractDeploymentInfo(BaseModel):
    chain_id: int
    network_name: str = "Local Hardhat / Dev EVM (Cancun)"
    token_address: str
    token_symbol: str = "COMPUTE"
    token_name: str = "ComputeCoin"
    token_total_supply: str = "1000000000000000000000000000"  # 1B COMPUTE (18 decimals)
    settlement_contract_address: str
    platform_signer_address: str
    treasury_address: str
    is_paused: bool = False


class CreateEscrowLockRequest(BaseModel):
    job_id: uuid.UUID
    provider_id: uuid.UUID
    locked_amount_units: str = Field(description="Amount locked in wei (minor units) as decimal string")
    deadline_seconds: int = Field(default=86400, ge=300, le=2592000)
    lock_tx_hash: str = Field(min_length=64, max_length=66)
    customer_address: str = Field(min_length=42, max_length=42)
    provider_address: str = Field(min_length=42, max_length=42)


class EscrowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    customer_id: uuid.UUID
    provider_id: uuid.UUID
    chain_id: int
    contract_address: str
    token_address: str
    job_bytes32: str
    locked_amount_units: str
    deadline: dt.datetime
    status: str
    lock_tx_hash: str
    settle_tx_hash: str | None = None
    refund_tx_hash: str | None = None
    final_cost_units: str | None = None
    platform_fee_units: str | None = None
    voucher_signature: str | None = None
    reconciled: bool
    settled_at: dt.datetime | None = None
    created_at: dt.datetime


class IssueSettlementVoucherRequest(BaseModel):
    job_id: uuid.UUID
    final_cost_units: str
    platform_fee_units: str


class SettlementVoucherResponse(BaseModel):
    job_id: uuid.UUID
    job_bytes32: str
    final_cost_units: str
    platform_fee_units: str
    chain_id: int
    contract_address: str
    signer_address: str
    signature: str


class ConfirmSettlementTxRequest(BaseModel):
    job_id: uuid.UUID
    settle_tx_hash: str = Field(min_length=64, max_length=66)
    block_number: int | None = None


class ConfirmRefundTxRequest(BaseModel):
    job_id: uuid.UUID
    refund_tx_hash: str = Field(min_length=64, max_length=66)
    block_number: int | None = None


class RecordOnChainTxRequest(BaseModel):
    chain_id: int
    tx_hash: str = Field(min_length=64, max_length=66)
    tx_type: str
    sender_address: str = Field(min_length=42, max_length=42)
    recipient_address: str | None = None
    amount_units: str | None = None
    token_symbol: str = "COMPUTE"
    block_number: int | None = None
    job_id: uuid.UUID | None = None
    metadata_json: dict[str, Any] | None = None


class OnChainTransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chain_id: int
    tx_hash: str
    tx_type: str
    sender_address: str
    recipient_address: str | None = None
    amount_units: str | None = None
    token_symbol: str
    block_number: int | None = None
    status: str
    reconciled_with_ledger: bool
    ledger_txn_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    created_at: dt.datetime


class OnChainReconciliationSummary(BaseModel):
    total_escrows: int
    active_locked_amount_units: str
    total_settled_amount_units: str
    total_fees_collected_units: str
    total_refunded_amount_units: str
    unreconciled_transactions_count: int
    ledger_parity_verified: bool
