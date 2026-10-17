// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

/**
 * @title ComputeSettlement
 * @notice Trustless, non-custodial escrow and settlement contract for ComputeCoin compute jobs.
 * @dev Reconciles off-chain computational jobs with on-chain cryptographic settlement vouchers.
 */
contract ComputeSettlement is ReentrancyGuard, Pausable, Ownable {
    using SafeERC20 for IERC20;

    IERC20 public immutable token;
    address public platformSigner;
    address public feeTreasury;

    struct EscrowLock {
        address customer;
        address provider;
        uint256 lockedAmount;
        uint256 deadline;
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
     */
    function getEscrow(address customer, bytes32 jobId)
        external
        view
        returns (
            address customerAddr,
            address provider,
            uint256 lockedAmount,
            uint256 deadline,
            bool settled
        )
    {
        EscrowLock storage lock = escrows[getEscrowId(customer, jobId)];
        return (lock.customer, lock.provider, lock.lockedAmount, lock.deadline, lock.settled);
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
            settled: false
        });

        token.safeTransferFrom(msg.sender, address(this), maxCost);
        emit BudgetLocked(jobId, msg.sender, provider, maxCost, deadline);
    }

    /**
     * @notice Settles a completed job with an authorized platform execution voucher.
     * @param customer Address of the customer who locked the budget.
     * @param jobId Unique identifier of the compute job.
     * @param finalCost Actual billable cost based on audited runtime seconds.
     * @param platformFee Protocol fee portion of the final cost.
     * @param signature Cryptographic signature by platformSigner verifying execution receipt.
     */
    function settleJob(
        address customer,
        bytes32 jobId,
        uint256 finalCost,
        uint256 platformFee,
        bytes memory signature
    ) external whenNotPaused nonReentrant {
        bytes32 escrowId = getEscrowId(customer, jobId);
        EscrowLock storage escrow = escrows[escrowId];
        require(escrow.customer != address(0), "Escrow not found");
        require(!escrow.settled, "Escrow already settled");
        require(finalCost <= escrow.lockedAmount, "Cost exceeds locked budget");
        require(platformFee <= finalCost, "Fee exceeds final cost");

        // Verify cryptographic voucher signature bound to customer, jobId, finalCost, fee
        bytes32 messageHash = keccak256(
            abi.encodePacked(customer, jobId, finalCost, platformFee, block.chainid, address(this))
        );
        bytes32 ethSignedMessageHash = MessageHashUtils.toEthSignedMessageHash(messageHash);
        address recoveredSigner = ECDSA.recover(ethSignedMessageHash, signature);
        require(recoveredSigner == platformSigner, "Invalid voucher signature");

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
     * @param customer Address of the customer who locked the budget.
     * @param jobId Unique identifier of the compute job.
     * @param signature Optional signature from platformSigner, or empty if after deadline.
     */
    function refundJob(address customer, bytes32 jobId, bytes memory signature)
        external
        nonReentrant
    {
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
            // Early refund requires platform cancellation voucher
            bytes32 messageHash = keccak256(
                abi.encodePacked("REFUND", customer, jobId, block.chainid, address(this))
            );
            bytes32 ethSignedMessageHash = MessageHashUtils.toEthSignedMessageHash(messageHash);
            address recoveredSigner = ECDSA.recover(ethSignedMessageHash, signature);
            require(recoveredSigner == platformSigner, "Invalid refund signature");
        }

        escrow.settled = true;
        uint256 refundAmount = escrow.lockedAmount;
        token.safeTransfer(escrow.customer, refundAmount);

        emit JobRefunded(jobId, escrow.customer, refundAmount);
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
