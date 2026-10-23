// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";

/**
 * @title ComputeSettlement
 * @notice Trustless, non-custodial escrow and settlement contract for ComputeCoin compute jobs.
 * @dev Reconciles off-chain computational jobs with on-chain cryptographic settlement vouchers.
 *
 * Fund-safety invariants enforced by this contract:
 *  - Tokens enter only through {lockBudget} and leave only through {settleJob} / {refundJob}.
 *    Refunds always pay `escrow.customer`, payouts always pay `escrow.provider`, both recorded at
 *    lock time and immutable thereafter.
 *  - The owner CAN rotate the signer and the fee treasury, and `platformFee` is routed to the
 *    treasury. The owner's reach into a locked balance is therefore not zero — it is bounded by
 *    {MAX_FEE_BPS}: at most that fraction of `finalCost` can ever reach the treasury, so neither a
 *    rogue owner nor a stolen platform signing key can confiscate the principal. The remainder of
 *    every lock can only go to `escrow.provider` or back to `escrow.customer`.
 *  - For every escrow: lockedAmount == providerPayout + platformFee + customerRefund (settle path)
 *    or lockedAmount == customerRefund (refund path), so token conservation holds globally.
 *  - Vouchers are replay-bound: each escrow carries a monotonically increasing `nonce` and every
 *    voucher additionally carries an `expiry`. A voucher is therefore single-use AND time-boxed,
 *    so a leaked or superseded voucher cannot be presented later (see {settleJob}).
 *
 * Solidity ^0.8 checked arithmetic is used throughout; there is no `unchecked` block.
 */
contract ComputeSettlement is ReentrancyGuard, Pausable, Ownable {
    using SafeERC20 for IERC20;

    IERC20 public immutable token;

    /// @notice Hard ceiling on the protocol fee, as a fraction of `finalCost` in basis points.
    /// @dev Immutable and deliberately not owner-settable. `platformFee` is the ONLY value in a
    ///      settlement that reaches an owner-controlled address (`feeTreasury`), so without a cap
    ///      the combination of {setFeeTreasury} + {setPlatformSigner} — or a single compromise of
    ///      the off-chain platform signing key — lets a voucher with `platformFee == finalCost ==
    ///      lockedAmount` sweep an entire escrow. With the cap, the worst case is a mispriced job.
    uint256 public constant MAX_FEE_BPS = 3000; // 30%
    uint256 private constant BPS_DENOMINATOR = 10_000;

    /// @notice Key authorized to sign settlement / early-refund vouchers.
    /// @dev May be an EOA or an ERC-1271 smart-contract signer (multisig), verified via
    ///      {SignatureChecker}, so the platform can migrate to a multisig without redeploying.
    address public platformSigner;

    address public feeTreasury;

    struct EscrowLock {
        address customer;
        address provider;
        uint256 lockedAmount;
        uint256 deadline;
        /// @dev Current voucher nonce. A voucher is only valid at exactly this value; consuming
        ///      or invalidating a voucher advances it, which is what makes replay impossible.
        uint256 nonce;
        bool settled;
    }

    mapping(bytes32 => EscrowLock) public escrows;

    event BudgetLocked(
        bytes32 indexed jobId,
        address indexed customer,
        address indexed provider,
        uint256 amount,
        uint256 deadline
    );

    event JobSettled(
        bytes32 indexed jobId,
        address indexed provider,
        uint256 providerPayout,
        uint256 platformFee,
        uint256 customerRefund
    );

    event JobRefunded(
        bytes32 indexed jobId,
        address indexed customer,
        uint256 refundAmount
    );

    event VouchersInvalidated(bytes32 indexed jobId, address indexed customer, uint256 newNonce);

    event PlatformSignerUpdated(address indexed oldSigner, address indexed newSigner);
    event FeeTreasuryUpdated(address indexed oldTreasury, address indexed newTreasury);

    /**
     * @param _token Address of the ComputeCoin ERC-20 settlement token.
     * @param _platformSigner Authorized public key that signs off-chain execution vouchers.
     * @param _feeTreasury Address receiving protocol platform fees.
     * @param _owner Governance / multisig admin address.
     */
    constructor(
        address _token,
        address _platformSigner,
        address _feeTreasury,
        address _owner
    ) Ownable(_owner) {
        require(_token != address(0), "Invalid token");
        require(_platformSigner != address(0), "Invalid signer");
        require(_feeTreasury != address(0), "Invalid treasury");

        token = IERC20(_token);
        platformSigner = _platformSigner;
        feeTreasury = _feeTreasury;
    }

    /**
     * @notice Computes deterministic escrow identifier binding customer to jobId,
     * preventing any third party from front-running or squatting another customer's jobId.
     */
    function getEscrowId(address customer, bytes32 jobId) public pure returns (bytes32) {
        return keccak256(abi.encodePacked(customer, jobId));
    }

    /**
     * @notice Query escrow state by customer and jobId.
     * @dev `nonce` is returned last so existing integrations that destructure the first five
     *      values keep compiling against the new ABI.
     */
    function getEscrow(address customer, bytes32 jobId)
        external
        view
        returns (
            address customerAddr,
            address provider,
            uint256 lockedAmount,
            uint256 deadline,
            bool settled,
            uint256 nonce
        )
    {
        EscrowLock storage lock = escrows[getEscrowId(customer, jobId)];
        return (
            lock.customer,
            lock.provider,
            lock.lockedAmount,
            lock.deadline,
            lock.settled,
            lock.nonce
        );
    }

    /**
     * @notice Current voucher nonce for an escrow; the platform must sign vouchers at this value.
     */
    function escrowNonce(address customer, bytes32 jobId) external view returns (uint256) {
        return escrows[getEscrowId(customer, jobId)].nonce;
    }

    /**
     * @notice Digest a settlement voucher commits to. Exposed so off-chain signers (and tests)
     * can reproduce the payload byte-for-byte rather than re-deriving it by hand.
     */
    function settlementVoucherHash(
        address customer,
        bytes32 jobId,
        uint256 finalCost,
        uint256 platformFee,
        uint256 nonce,
        uint256 expiry
    ) public view returns (bytes32) {
        return
            keccak256(
                abi.encodePacked(
                    customer,
                    jobId,
                    finalCost,
                    platformFee,
                    nonce,
                    expiry,
                    block.chainid,
                    address(this)
                )
            );
    }

    /**
     * @notice Digest an early-refund (cancellation) voucher commits to.
     */
    function refundVoucherHash(
        address customer,
        bytes32 jobId,
        uint256 nonce,
        uint256 expiry
    ) public view returns (bytes32) {
        return
            keccak256(
                abi.encodePacked(
                    "REFUND",
                    customer,
                    jobId,
                    nonce,
                    expiry,
                    block.chainid,
                    address(this)
                )
            );
    }

    /**
     * @notice Locks customer compute tokens into escrow for an approved job/reservation.
     * Keyed by keccak256(msg.sender, jobId) so third parties cannot squat jobIds.
     * @param jobId Unique identifier of the compute job.
     * @param provider Address of the matched GPU provider machine owner.
     * @param maxCost Maximum budget ceiling authorized for the job.
     * @param deadline Unix timestamp after which customer can self-refund if not settled.
     */
    function lockBudget(
        bytes32 jobId,
        address provider,
        uint256 maxCost,
        uint256 deadline
    ) external whenNotPaused nonReentrant {
        require(jobId != bytes32(0), "Invalid job id");
        require(provider != address(0), "Invalid provider");
        require(maxCost > 0, "Max cost must be > 0");
        require(deadline >= block.timestamp, "Deadline must be future or current");

        bytes32 escrowId = getEscrowId(msg.sender, jobId);
        require(escrows[escrowId].customer == address(0), "Escrow already exists");

        escrows[escrowId] = EscrowLock({
            customer: msg.sender,
            provider: provider,
            lockedAmount: maxCost,
            deadline: deadline,
            nonce: 0,
            settled: false
        });

        token.safeTransferFrom(msg.sender, address(this), maxCost);
        emit BudgetLocked(jobId, msg.sender, provider, maxCost, deadline);
    }

    /**
     * @notice Settles a completed job with an authorized platform execution voucher.
     * @dev The voucher binds (customer, jobId, finalCost, platformFee, nonce, expiry, chainId,
     *      contract). `nonce` makes it single-use — it must equal the escrow's current nonce and
     *      is advanced on consumption — and `expiry` time-boxes it, so a voucher that was signed
     *      but never broadcast (or one superseded by {invalidateVouchers}) cannot be replayed
     *      later against the same escrow. The off-chain signer in
     *      `apps/api/app/services/settlement.py` must pack these fields in exactly this order.
     * @param customer Address of the customer who locked the budget.
     * @param jobId Unique identifier of the compute job.
     * @param finalCost Actual billable cost based on audited runtime seconds.
     * @param platformFee Protocol fee portion of the final cost.
     * @param nonce Voucher nonce; must equal the escrow's current nonce.
     * @param expiry Unix timestamp after which the voucher is no longer honored.
     * @param signature Signature by platformSigner (EOA or ERC-1271) over the voucher digest.
     */
    function settleJob(
        address customer,
        bytes32 jobId,
        uint256 finalCost,
        uint256 platformFee,
        uint256 nonce,
        uint256 expiry,
        bytes memory signature
    ) external whenNotPaused nonReentrant {
        bytes32 escrowId = getEscrowId(customer, jobId);
        EscrowLock storage escrow = escrows[escrowId];
        require(escrow.customer != address(0), "Escrow not found");
        require(!escrow.settled, "Escrow already settled");
        require(finalCost <= escrow.lockedAmount, "Cost exceeds locked budget");
        require(platformFee <= finalCost, "Fee exceeds final cost");
        require(platformFee * BPS_DENOMINATOR <= finalCost * MAX_FEE_BPS, "Fee above cap");
        require(block.timestamp <= expiry, "Voucher expired");
        require(nonce == escrow.nonce, "Stale voucher nonce");

        bytes32 digest = MessageHashUtils.toEthSignedMessageHash(
            settlementVoucherHash(customer, jobId, finalCost, platformFee, nonce, expiry)
        );
        require(
            SignatureChecker.isValidSignatureNow(platformSigner, digest, signature),
            "Invalid voucher signature"
        );

        // Effects before interactions: burn the nonce and close the escrow first.
        escrow.nonce = nonce + 1;
        escrow.settled = true;

        uint256 providerPayout = finalCost - platformFee;
        uint256 customerRefund = escrow.lockedAmount - finalCost;

        if (providerPayout > 0) {
            token.safeTransfer(escrow.provider, providerPayout);
        }
        if (platformFee > 0) {
            token.safeTransfer(feeTreasury, platformFee);
        }
        if (customerRefund > 0) {
            token.safeTransfer(escrow.customer, customerRefund);
        }

        emit JobSettled(jobId, escrow.provider, providerPayout, platformFee, customerRefund);
    }

    /**
     * @notice Refunds locked tokens to customer if job was cancelled or expired.
     * @dev Deliberately NOT `whenNotPaused`: pausing must never trap customer funds. After the
     *      escrow deadline the customer (or owner, on their behalf) can always withdraw to the
     *      customer address with no voucher at all. Before the deadline an early-refund voucher is
     *      required, and that voucher carries the same nonce + expiry replay protection as a
     *      settlement voucher.
     * @param customer Address of the customer who locked the budget.
     * @param jobId Unique identifier of the compute job.
     * @param nonce Voucher nonce; must equal the escrow's current nonce (ignored after deadline).
     * @param expiry Unix timestamp after which the voucher is void (ignored after deadline).
     * @param signature Platform cancellation voucher; may be empty once the deadline has passed.
     */
    function refundJob(
        address customer,
        bytes32 jobId,
        uint256 nonce,
        uint256 expiry,
        bytes memory signature
    ) external nonReentrant {
        bytes32 escrowId = getEscrowId(customer, jobId);
        EscrowLock storage escrow = escrows[escrowId];
        require(escrow.customer != address(0), "Escrow not found");
        require(!escrow.settled, "Escrow already settled");

        if (block.timestamp >= escrow.deadline) {
            // Customer can self-refund after deadline expired
            require(
                msg.sender == escrow.customer || msg.sender == owner(),
                "Only customer or owner after deadline"
            );
        } else {
            // Early refund requires a platform cancellation voucher
            require(block.timestamp <= expiry, "Voucher expired");
            require(nonce == escrow.nonce, "Stale voucher nonce");

            bytes32 digest = MessageHashUtils.toEthSignedMessageHash(
                refundVoucherHash(customer, jobId, nonce, expiry)
            );
            require(
                SignatureChecker.isValidSignatureNow(platformSigner, digest, signature),
                "Invalid refund signature"
            );
            escrow.nonce = nonce + 1;
        }

        escrow.settled = true;
        uint256 refundAmount = escrow.lockedAmount;
        // Always pays the recorded customer, never msg.sender — the owner-assisted path cannot
        // be used to divert funds.
        token.safeTransfer(escrow.customer, refundAmount);

        emit JobRefunded(jobId, escrow.customer, refundAmount);
    }

    /**
     * @notice Revokes every outstanding voucher for an escrow by advancing its nonce.
     * @dev Needed for signer-key rotation and for retracting a voucher that was signed with wrong
     *      billing data. This is purely a *negative* power: it can only stop a payout, never move
     *      tokens. The customer's post-deadline self-refund path stays open regardless.
     */
    function invalidateVouchers(address customer, bytes32 jobId) external onlyOwner {
        EscrowLock storage escrow = escrows[getEscrowId(customer, jobId)];
        require(escrow.customer != address(0), "Escrow not found");
        require(!escrow.settled, "Escrow already settled");
        escrow.nonce = escrow.nonce + 1;
        emit VouchersInvalidated(jobId, customer, escrow.nonce);
    }

    function setPlatformSigner(address newSigner) external onlyOwner {
        require(newSigner != address(0), "Invalid signer");
        emit PlatformSignerUpdated(platformSigner, newSigner);
        platformSigner = newSigner;
    }

    function setFeeTreasury(address newTreasury) external onlyOwner {
        require(newTreasury != address(0), "Invalid treasury");
        emit FeeTreasuryUpdated(feeTreasury, newTreasury);
        feeTreasury = newTreasury;
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }
}
