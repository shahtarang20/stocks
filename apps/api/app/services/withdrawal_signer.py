"""Who signs a payout transaction (B1). The key is NEVER in a file, a setting or the database.

``WithdrawalSigner`` is the injectable interface. The production implementation, ``KmsWithdrawalSigner``,
talks to a KMS / HSM through a ``KmsAdapter``. ``AwsKmsAdapter`` is the AWS KMS adapter (the product
choice still needs owner approval, docs/architecture/B1-withdrawals-design.md); ``StubKmsAdapter`` is
the default when none is passed and raises ``NotConfigured``.
Tests inject a local signer; nothing here ever reads a private key from the environment.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from app.core.config import settings

SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


class NotConfigured(RuntimeError):
    """No signer is configured. Withdrawals stay unsent; requests wait or are refunded."""


class SignerError(RuntimeError):
    """The signer refused or failed. Nothing was broadcast."""


class WithdrawalSigner(Protocol):
    @property
    def address(self) -> str:
        """The hot wallet address (lower-case) this signer signs for."""
        ...

    async def sign_transaction(self, unsigned_tx: dict[str, Any]) -> str:
        """Sign a legacy transaction dict (chainId, nonce, gasPrice, gas, to, value, data).
        Returns the raw signed transaction as 0x-hex."""
        ...


class KmsAdapter(Protocol):
    """Minimal KMS / HSM surface: sign a 32-byte digest with a secp256k1 key that never leaves the module."""

    async def public_address(self, key_id: str) -> str: ...

    async def sign_digest(self, key_id: str, digest: bytes) -> tuple[int, int, int]:
        """Returns (v, r, s) for the digest, v in {27, 28} or EIP-155 form as the adapter documents."""
        ...


class StubKmsAdapter:
    """Placeholder until the owner picks a KMS / HSM. Every call raises NotConfigured."""

    async def public_address(self, key_id: str) -> str:
        raise NotConfigured("withdrawal_kms_not_configured")

    async def sign_digest(self, key_id: str, digest: bytes) -> tuple[int, int, int]:
        raise NotConfigured("withdrawal_kms_not_configured")


def _der_len(buf: bytes, i: int) -> tuple[int, int]:
    first = buf[i]
    if first < 0x80:
        return first, i + 1
    n = first & 0x7F
    return int.from_bytes(buf[i + 1 : i + 1 + n], "big"), i + 1 + n


def parse_der_signature(der: bytes) -> tuple[int, int]:
    """DER ``SEQUENCE { INTEGER r, INTEGER s }`` (what KMS returns) -> (r, s). Strict, no trailing bytes."""
    try:
        if der[0] != 0x30:
            raise ValueError("not a sequence")
        seq_len, i = _der_len(der, 1)
        if i + seq_len != len(der):
            raise ValueError("bad length")
        out = []
        for _ in range(2):
            if der[i] != 0x02:
                raise ValueError("not an integer")
            n, i = _der_len(der, i + 1)
            out.append(int.from_bytes(der[i : i + n], "big"))
            i += n
        if i != len(der):
            raise ValueError("trailing bytes")
        return out[0], out[1]
    except (IndexError, ValueError) as exc:
        raise SignerError("kms_signature_malformed") from exc


def address_from_spki(spki_der: bytes) -> str:
    """Ethereum address (lower-case) from a DER SubjectPublicKeyInfo of a secp256k1 key.
    The BIT STRING ends with the 65-byte uncompressed point 0x04 || X || Y."""
    from eth_utils import keccak

    point = spki_der[-65:]
    if len(point) != 65 or point[0] != 4:
        raise SignerError("kms_public_key_malformed")
    return "0x" + keccak(point[1:])[-20:].hex()


def normalise_low_s(r: int, s: int) -> tuple[int, int]:
    """Ethereum rejects high-s signatures (EIP-2): replace s by n - s when it is above n/2."""
    if s > SECP256K1_N // 2:
        s = SECP256K1_N - s
    return r, s


def recover_v(digest: bytes, r: int, s: int, expected_address: str) -> int:
    """Try recovery id 0 and 1; return the one whose recovered key is ``expected_address`` (as 27/28)."""
    from eth_keys import keys

    want = expected_address.lower()
    for rec in (0, 1):
        try:
            sig = keys.Signature(vrs=(rec, r, s))
            if sig.recover_public_key_from_msg_hash(digest).to_address().lower() == want:
                return 27 + rec
        except Exception:  # an invalid (r, s, rec) combination: try the other id
            continue
    raise SignerError("kms_signature_does_not_recover_to_hot_wallet")


class AwsKmsAdapter:
    """AWS KMS: an asymmetric ECC_SECG_P256K1 key with usage SIGN_VERIFY; the key never leaves KMS.

    boto3 is imported lazily (only when this adapter is used) and its blocking calls run in a thread.
    Checked 2026-11-03 against https://docs.aws.amazon.com/kms/latest/APIReference/API_Sign.html and
    API_GetPublicKey.html: ``MessageType="DIGEST"`` skips hashing (length must match the hash, 32 bytes
    for ECDSA_SHA_256), the signature is DER (RFC 3279 2.2.3), ``PublicKey`` is DER SubjectPublicKeyInfo,
    ECC_SECG_P256K1 is a documented KeySpec. Not yet run against a real key.
    """

    def __init__(self, region: str | None = None, client: Any = None) -> None:
        self._region = (region if region is not None else settings.WITHDRAWALS_KMS_REGION).strip()
        self._client = client
        self._addr: dict[str, str] = {}

    def _kms(self) -> Any:
        if self._client is None:
            try:
                import boto3  # lazy: only needed when KMS signing is really used
            except ImportError as exc:
                raise NotConfigured("boto3_not_installed") from exc
            self._client = boto3.client("kms", region_name=self._region or None)
        return self._client

    async def public_address(self, key_id: str) -> str:
        if key_id not in self._addr:
            try:
                resp = await asyncio.to_thread(self._kms().get_public_key, KeyId=key_id)
            except NotConfigured:
                raise
            except Exception as exc:
                raise SignerError("kms_get_public_key_failed") from exc
            self._addr[key_id] = address_from_spki(bytes(resp["PublicKey"]))
        return self._addr[key_id]

    async def sign_digest(self, key_id: str, digest: bytes) -> tuple[int, int, int]:
        """Returns (v, r, s) with v in {27, 28}, s normalised to the lower half of the curve order."""
        if len(digest) != 32:
            raise SignerError("digest_must_be_32_bytes")
        known = await self.public_address(key_id)
        try:
            resp = await asyncio.to_thread(
                self._kms().sign, KeyId=key_id, Message=digest, MessageType="DIGEST", SigningAlgorithm="ECDSA_SHA_256"
            )
        except NotConfigured:
            raise
        except Exception as exc:
            raise SignerError("kms_sign_failed") from exc
        r, s = normalise_low_s(*parse_der_signature(bytes(resp["Signature"])))
        return recover_v(digest, r, s, known), r, s


class KmsWithdrawalSigner:
    """Production signer: builds the transaction hash locally and asks the KMS adapter to sign it."""

    def __init__(self, adapter: KmsAdapter | None = None, key_id: str | None = None) -> None:
        self._adapter = adapter or StubKmsAdapter()
        self._key_id = (key_id if key_id is not None else settings.WITHDRAWALS_KMS_KEY_ID).strip()
        if not self._key_id:
            raise NotConfigured("withdrawal_kms_key_id_missing")
        self._address = settings.WITHDRAWALS_HOT_WALLET_ADDRESS.strip().lower()

    @property
    def address(self) -> str:
        return self._address

    async def sign_transaction(self, unsigned_tx: dict[str, Any]) -> str:
        """Sign a LEGACY (EIP-155) transaction with the KMS key; returns the raw tx as 0x-hex.

        TODO(verify-docs): UNVERIFIABLE as public API. eth-account's docs
        (https://eth-account.readthedocs.io/en/stable/eth_account.html, 2026-11-03) document no way to
        hash an unsigned tx or serialize one with an external signature; ``serializable_unsigned_transaction_from_dict``
        and ``encode_transaction`` live in the private ``eth_account._utils.legacy_transactions``. Their
        signatures were confirmed in the installed eth-account 0.14.0 source only: pin the version. They are
        the same helpers ``Account.sign_transaction`` uses internally; the
        resulting raw bytes must equal what ``Account.sign_transaction`` gives for the same key. This is
        checked by tests/test_b1_withdrawals_live.py (opt-in) and the unit test with a local key.
        Typed (EIP-1559) transactions are refused until their entry point is verified.
        """
        if "maxFeePerGas" in unsigned_tx or unsigned_tx.get("type") not in (None, 0, "0x0"):
            raise SignerError("only_legacy_transactions_supported")
        from eth_account._utils.legacy_transactions import (
            encode_transaction,
            serializable_unsigned_transaction_from_dict,
        )

        chain_id = int(unsigned_tx["chainId"])
        fields = {k: v for k, v in unsigned_tx.items() if k not in ("from", "type")}
        if isinstance(fields.get("to"), str) and fields["to"]:
            from eth_utils import to_checksum_address

            fields["to"] = to_checksum_address(fields["to"])  # eth-account refuses non-checksummed addresses
        unsigned = serializable_unsigned_transaction_from_dict(fields)
        digest = bytes(unsigned.hash())  # keccak256 of the EIP-155 unsigned RLP
        derived = (await self._adapter.public_address(self._key_id)).lower()
        if derived != self._address:
            # The KMS key is not the configured hot wallet: refuse rather than pay from the wrong address.
            raise SignerError("kms_key_does_not_match_hot_wallet")
        v, r, s = await self._adapter.sign_digest(self._key_id, digest)
        if v in (27, 28):
            v = v - 27 + chain_id * 2 + 35  # EIP-155
        signed = encode_transaction(unsigned, vrs=(v, r, s))
        return "0x" + bytes(signed).hex()


def build_default_signer() -> WithdrawalSigner:
    """The signer the worker uses: AWS KMS. Raises NotConfigured when no key id / hot wallet is set."""
    return KmsWithdrawalSigner(adapter=AwsKmsAdapter())
