// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/**
 * @title ComputeStakeVault
 * @notice Host stake for ComputeCoin Proof-of-Privacy (ADR-0005): bonding, unbonding, slashing with
 *         customer compensation, and anchoring of the transparency-log root.
 * @dev Fund-safety rules this contract enforces:
 *  - A machine id is bound to its provider by the owner (`registerMachine`), so nobody can occupy
 *    another host's id first and lock the real host out.
 *  - Slashing is two-step: the slasher PROPOSES (which freezes the machine's stake, so the host
 *    cannot unbond away from it), anyone may inspect it during `slashDelay`, the owner may CANCEL
 *    it (appeal upheld), and only after the delay can it be EXECUTED. A stolen slasher key can
 *    therefore not drain stakes instantly.
 *  - Every slashed token goes somewhere: compensation needs a real recipient, the rest goes to
 *    the insurance pool. What is not slashed from a banned machine becomes withdrawable after
 *    the unbonding period instead of being locked forever.
 *  - The unbonding period can never be shorter than `MIN_UNBONDING_PERIOD`, and is always longer
 *    than the slash delay, so a proposed slash can always be executed before the stake leaves.
 */
contract ComputeStakeVault is ReentrancyGuard, Pausable, Ownable {
    using SafeERC20 for IERC20;

    IERC20 public immutable token;

    uint256 public constant MIN_SEALED_STAKE = 1_000 * 10 ** 18;
    uint256 public constant MIN_SEALED_PRO_STAKE = 5_000 * 10 ** 18;
    uint256 public constant MIN_UNBONDING_PERIOD = 3 days;
    uint256 public constant MAX_UNBONDING_PERIOD = 30 days;
    uint256 public constant MIN_SLASH_DELAY = 1 days;

    uint256 public unbondingPeriod = 7 days;
    uint256 public slashDelay = 2 days;

    address public insurancePool;
    address public slasher;
    address public anchorSigner;

    enum Tier {
        Open,
        Sealed,
        SealedPro,
        Confidential
    }

    struct MachineStake {
        address provider;
        uint256 bondedAmount;
        uint256 unbondingAmount;
        uint256 unbondingAvailableAt;
        bool isBanned;
    }

    struct PendingSlash {
        address customerRecipient;
        uint256 customerCompensation;
        uint256 slashAmount;
        uint256 executeAfter;
        string reason;
    }

    mapping(bytes32 => MachineStake) public stakes;
    mapping(bytes32 => PendingSlash) public pendingSlashes;
    mapping(address => uint256) public totalProviderBonded;

    mapping(bytes32 => uint256) public merkleRoots;
    bytes32 public latestMerkleRoot;
    uint256 public totalAnchoredLeaves;

    event MachineRegistered(bytes32 indexed machineId, address indexed provider);
    event StakeDeposited(bytes32 indexed machineId, address indexed provider, uint256 amount, uint256 newTotal);
    event UnbondingRequested(bytes32 indexed machineId, address indexed provider, uint256 amount, uint256 availableAt);
    event StakeWithdrawn(bytes32 indexed machineId, address indexed provider, uint256 amount);
    event SlashProposed(bytes32 indexed machineId, address indexed customerRecipient, uint256 customerCompensation, uint256 slashAmount, uint256 executeAfter, string reason);
    event SlashCancelled(bytes32 indexed machineId);
    event MachineSlashed(
        bytes32 indexed machineId,
        address indexed provider,
        address indexed customerRecipient,
        uint256 customerCompensation,
        uint256 penaltyToInsurance,
        string reason
    );
    event MerkleRootAnchored(bytes32 indexed rootHash, uint256 leafCount, uint256 blockNumber, uint256 timestamp);
    event InsurancePoolUpdated(address indexed oldPool, address indexed newPool);
    event SlasherUpdated(address indexed oldSlasher, address indexed newSlasher);
    event AnchorSignerUpdated(address indexed oldSigner, address indexed newSigner);
    event UnbondingPeriodUpdated(uint256 oldPeriod, uint256 newPeriod);
    event SlashDelayUpdated(uint256 oldDelay, uint256 newDelay);

    modifier onlySlasherOrOwner() {
        require(msg.sender == slasher || msg.sender == owner(), "ComputeStakeVault: caller is not slasher or owner");
        _;
    }

    modifier onlyAnchorOrOwner() {
        require(msg.sender == anchorSigner || msg.sender == owner(), "ComputeStakeVault: caller is not anchor or owner");
        _;
    }

    modifier notFrozen(bytes32 machineId) {
        require(pendingSlashes[machineId].executeAfter == 0, "Stake is frozen by a pending slash");
        _;
    }

    constructor(address tokenAddress, address initialOwner, address initialInsurancePool, address initialSlasher)
        Ownable(initialOwner)
    {
        require(tokenAddress != address(0), "Invalid token address");
        require(initialInsurancePool != address(0), "Invalid insurance pool");
        require(initialSlasher != address(0), "Invalid slasher address");
        token = IERC20(tokenAddress);
        insurancePool = initialInsurancePool;
        slasher = initialSlasher;
        anchorSigner = initialOwner;
    }

    // ------------------------------------------------------------------------
    // Machine registration and staking
    // ------------------------------------------------------------------------

    /// @notice Bind a platform machine id to the provider address allowed to stake for it.
    function registerMachine(bytes32 machineId, address provider) external onlyOwner {
        require(machineId != bytes32(0), "Invalid machineId");
        require(provider != address(0), "Invalid provider");
        MachineStake storage m = stakes[machineId];
        require(m.provider == address(0), "Machine already registered");
        m.provider = provider;
        emit MachineRegistered(machineId, provider);
    }

    function depositStake(bytes32 machineId, uint256 amount) external nonReentrant whenNotPaused notFrozen(machineId) {
        require(amount > 0, "Amount must be greater than zero");
        MachineStake storage m = stakes[machineId];
        require(m.provider != address(0), "Machine not registered");
        require(m.provider == msg.sender, "Only machine owner can add stake");
        require(!m.isBanned, "Machine is banned from network");

        m.bondedAmount += amount;
        totalProviderBonded[msg.sender] += amount;
        token.safeTransferFrom(msg.sender, address(this), amount);
        emit StakeDeposited(machineId, msg.sender, amount, m.bondedAmount);
    }

    function requestUnstake(bytes32 machineId, uint256 amount) external nonReentrant notFrozen(machineId) {
        require(amount > 0, "Amount must be greater than zero");
        MachineStake storage m = stakes[machineId];
        require(m.provider == msg.sender, "Not machine owner");
        require(!m.isBanned, "Machine is banned");
        require(m.bondedAmount >= amount, "Insufficient bonded stake");

        m.bondedAmount -= amount;
        totalProviderBonded[msg.sender] -= amount;
        m.unbondingAmount += amount;
        m.unbondingAvailableAt = block.timestamp + unbondingPeriod;
        emit UnbondingRequested(machineId, msg.sender, amount, m.unbondingAvailableAt);
    }

    /// @notice Withdraw unbonded stake after the cooldown. Allowed for banned machines too: what
    ///         was not slashed belongs to the provider.
    function withdrawStake(bytes32 machineId) external nonReentrant notFrozen(machineId) {
        MachineStake storage m = stakes[machineId];
        require(m.provider == msg.sender, "Not machine owner");
        require(m.unbondingAmount > 0, "No unbonding stake");
        require(block.timestamp >= m.unbondingAvailableAt, "Unbonding period not elapsed");

        uint256 amount = m.unbondingAmount;
        m.unbondingAmount = 0;
        token.safeTransfer(msg.sender, amount);
        emit StakeWithdrawn(machineId, msg.sender, amount);
    }

    // ------------------------------------------------------------------------
    // Slashing: propose -> (delay, owner may cancel) -> execute
    // ------------------------------------------------------------------------

    function proposeSlash(
        bytes32 machineId,
        address customerRecipient,
        uint256 customerCompensation,
        uint256 slashAmount,
        string calldata reason
    ) external onlySlasherOrOwner {
        MachineStake storage m = stakes[machineId];
        require(m.provider != address(0), "Machine has no stake");
        require(!m.isBanned, "Machine already slashed and banned");
        require(pendingSlashes[machineId].executeAfter == 0, "Slash already pending");
        require(slashAmount > 0, "Slash amount must be greater than zero");
        require(customerCompensation <= slashAmount, "Compensation exceeds slash amount");
        require(customerCompensation == 0 || customerRecipient != address(0), "Compensation needs a recipient");

        uint256 executeAfter = block.timestamp + slashDelay;
        pendingSlashes[machineId] = PendingSlash(customerRecipient, customerCompensation, slashAmount, executeAfter, reason);
        emit SlashProposed(machineId, customerRecipient, customerCompensation, slashAmount, executeAfter, reason);
    }

    /// @notice The owner upholds the host's appeal. The stake is unfrozen.
    function cancelSlash(bytes32 machineId) external onlyOwner {
        require(pendingSlashes[machineId].executeAfter != 0, "No pending slash");
        delete pendingSlashes[machineId];
        emit SlashCancelled(machineId);
    }

    function executeSlash(bytes32 machineId) external nonReentrant onlySlasherOrOwner {
        PendingSlash memory p = pendingSlashes[machineId];
        require(p.executeAfter != 0, "No pending slash");
        require(block.timestamp >= p.executeAfter, "Slash delay not elapsed");
        delete pendingSlashes[machineId];

        MachineStake storage m = stakes[machineId];
        uint256 available = m.bondedAmount + m.unbondingAmount;
        uint256 slashAmount = p.slashAmount > available ? available : p.slashAmount;
        uint256 compensation = p.customerCompensation > slashAmount ? slashAmount : p.customerCompensation;

        // Take from bonded first, then from unbonding.
        uint256 fromBonded = slashAmount > m.bondedAmount ? m.bondedAmount : slashAmount;
        m.bondedAmount -= fromBonded;
        totalProviderBonded[m.provider] -= fromBonded;
        m.unbondingAmount -= slashAmount - fromBonded;

        // Ban, and release what was NOT slashed through the normal unbonding path.
        m.isBanned = true;
        if (m.bondedAmount > 0) {
            totalProviderBonded[m.provider] -= m.bondedAmount;
            m.unbondingAmount += m.bondedAmount;
            m.bondedAmount = 0;
            m.unbondingAvailableAt = block.timestamp + unbondingPeriod;
        }

        uint256 penalty = slashAmount - compensation;
        if (compensation > 0) token.safeTransfer(p.customerRecipient, compensation);
        if (penalty > 0) token.safeTransfer(insurancePool, penalty);
        emit MachineSlashed(machineId, m.provider, p.customerRecipient, compensation, penalty, p.reason);
    }

    // ------------------------------------------------------------------------
    // Transparency log anchoring
    // ------------------------------------------------------------------------

    /// @notice Anchor the transparency-log root for a given size. The size must grow; re-anchoring
    ///         the same size is only accepted with the same root, so history cannot be rewritten.
    function commitMerkleRoot(bytes32 rootHash, uint256 leafCount) external onlyAnchorOrOwner {
        require(rootHash != bytes32(0), "Invalid Merkle root hash");
        require(
            leafCount > totalAnchoredLeaves || (leafCount == totalAnchoredLeaves && rootHash == latestMerkleRoot),
            "Leaf count must grow (same size needs the same root)"
        );
        if (merkleRoots[rootHash] == 0) merkleRoots[rootHash] = block.timestamp;
        latestMerkleRoot = rootHash;
        totalAnchoredLeaves = leafCount;
        emit MerkleRootAnchored(rootHash, leafCount, block.number, block.timestamp);
    }

    // ------------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------------

    function isEligibleForTier(bytes32 machineId, Tier requiredTier) external view returns (bool) {
        MachineStake storage m = stakes[machineId];
        if (m.isBanned || pendingSlashes[machineId].executeAfter != 0) return false;
        if (requiredTier == Tier.Open || requiredTier == Tier.Confidential) return true;
        if (requiredTier == Tier.Sealed) return m.bondedAmount >= MIN_SEALED_STAKE;
        return m.bondedAmount >= MIN_SEALED_PRO_STAKE;
    }

    function getBondedStake(bytes32 machineId) external view returns (uint256) {
        return stakes[machineId].bondedAmount;
    }

    // ------------------------------------------------------------------------
    // Admin
    // ------------------------------------------------------------------------

    function setInsurancePool(address newPool) external onlyOwner {
        require(newPool != address(0), "Invalid insurance pool");
        emit InsurancePoolUpdated(insurancePool, newPool);
        insurancePool = newPool;
    }

    function setSlasher(address newSlasher) external onlyOwner {
        require(newSlasher != address(0), "Invalid slasher");
        emit SlasherUpdated(slasher, newSlasher);
        slasher = newSlasher;
    }

    function setAnchorSigner(address newSigner) external onlyOwner {
        require(newSigner != address(0), "Invalid anchor signer");
        emit AnchorSignerUpdated(anchorSigner, newSigner);
        anchorSigner = newSigner;
    }

    function setUnbondingPeriod(uint256 newPeriod) external onlyOwner {
        require(newPeriod >= MIN_UNBONDING_PERIOD && newPeriod <= MAX_UNBONDING_PERIOD, "Period out of range");
        require(newPeriod > slashDelay, "Unbonding must outlast the slash delay");
        emit UnbondingPeriodUpdated(unbondingPeriod, newPeriod);
        unbondingPeriod = newPeriod;
    }

    function setSlashDelay(uint256 newDelay) external onlyOwner {
        require(newDelay >= MIN_SLASH_DELAY && newDelay < unbondingPeriod, "Delay out of range");
        emit SlashDelayUpdated(slashDelay, newDelay);
        slashDelay = newDelay;
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }
}
