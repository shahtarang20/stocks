// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ComputeCoinToken} from "../src/ComputeCoinToken.sol";
import {ComputeSettlement} from "../src/ComputeSettlement.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

contract EscrowSquatterHelper {
    function squat(
        ComputeSettlement settlement,
        ComputeCoinToken token,
        bytes32 jobId,
        address provider,
        uint256 cost
    ) external {
        token.approve(address(settlement), cost);
        settlement.lockBudget(jobId, provider, cost, block.timestamp + 3600);
    }
}

/**
 * @dev Customer wallet stand-in used to prove the owner-assisted refund path pays the recorded
 *      customer and not the caller. Keeps its own balance so a seizure would be observable.
 */
contract CustomerWallet {
    function lock(
        ComputeSettlement settlement,
        ComputeCoinToken token,
        bytes32 jobId,
        address provider,
        uint256 cost,
        uint256 deadline
    ) external {
        token.approve(address(settlement), cost);
        settlement.lockBudget(jobId, provider, cost, deadline);
    }
}

/**
 * @dev ERC-1271 signer used by the Solidity harness. Hardhat's Solidity test runner has no
 *      `vm.sign` cheatcode, so a *valid* voucher cannot be produced from an EOA private key
 *      inside a test. ComputeSettlement verifies vouchers with OpenZeppelin's SignatureChecker,
 *      which routes contract signers through ERC-1271 — so pointing `platformSigner` at this
 *      mock gives the harness deterministic accept/reject control and lets the invariant loops
 *      execute real settlements. EOA verification is exercised separately by the
 *      "invalid signature" tests and end-to-end by tests/test_phase10_settlement.py.
 */
contract MockVoucherSigner {
    bytes4 private constant MAGIC = 0x1626ba7e;
    bool public immutable accepts;

    constructor(bool accepts_) {
        accepts = accepts_;
    }

    function isValidSignature(bytes32, bytes memory) external view returns (bytes4) {
        return accepts ? MAGIC : bytes4(0xffffffff);
    }
}

contract ComputeSettlementTest {
    ComputeCoinToken public token;
    ComputeSettlement public settlement;

    address public owner;
    address public platformSigner;
    uint256 public platformSignerPrivateKey;
    address public feeTreasury;
    address public customer;
    address public provider;

    MockVoucherSigner public goodSigner;
    MockVoucherSigner public badSigner;

    uint256 public constant ONE_HOUR = 3600;
    bytes internal constant ANY_SIG = hex"00";

    function setUp() public {
        owner = address(this);
        platformSignerPrivateKey = 0xA11CE;
        platformSigner = 0xe05fcC23807536bEe418f142D19fa0d21BB0cfF7;
        feeTreasury = address(0x2222);
        customer = address(0x3333);
        provider = address(0x4444);

        token = new ComputeCoinToken(owner);
        settlement = new ComputeSettlement(
            address(token),
            platformSigner,
            feeTreasury,
            owner
        );

        goodSigner = new MockVoucherSigner(true);
        badSigner = new MockVoucherSigner(false);

        // Fund customer
        token.transfer(customer, 10_000 * 10 ** 18);
    }

    function test_token_genesis_and_metadata() public view {
        require(token.totalSupply() == 1_000_000_000 * 10 ** 18, "Wrong total supply");
        require(keccak256(bytes(token.symbol())) == keccak256(bytes("COMPUTE")), "Wrong symbol");
        require(keccak256(bytes(token.name())) == keccak256(bytes("ComputeCoin")), "Wrong name");
    }

    function test_token_burn() public {
        token.transfer(customer, 500 * 10 ** 18);
        uint256 supplyBefore = token.totalSupply();
        token.burn(100 * 10 ** 18);
        require(token.totalSupply() == supplyBefore - 100 * 10 ** 18, "Burn did not reduce supply");
    }

    function test_token_pause_circuit_breaker() public {
        token.pause();
        bool failed = false;
        try token.transfer(customer, 10) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "Transfer should fail when paused");

        token.unpause();
        token.transfer(customer, 10);
        require(token.balanceOf(customer) >= 10, "Transfer succeeded after unpause");
    }

    function test_escrow_lock_budget() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-001"));
        uint256 maxCost = 50 * 10 ** 18;
        uint256 deadline = block.timestamp + ONE_HOUR;

        token.approve(address(settlement), maxCost);
        settlement.lockBudget(jobId, provider, maxCost, deadline);

        (address c, address p, uint256 amt, uint256 dl, bool settled, uint256 nonce) =
            settlement.getEscrow(address(this), jobId);
        require(c == address(this), "Customer mismatch");
        require(p == provider, "Provider mismatch");
        require(amt == maxCost, "Amount mismatch");
        require(dl == deadline, "Deadline mismatch");
        require(!settled, "Must not be settled");
        require(nonce == 0, "Fresh escrow must start at nonce 0");
        require(token.balanceOf(address(settlement)) == maxCost, "Escrow balance mismatch");
    }

    function test_escrow_double_lock_reverts() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-002"));
        uint256 maxCost = 50 * 10 ** 18;
        uint256 deadline = block.timestamp + ONE_HOUR;

        token.approve(address(settlement), maxCost * 2);
        settlement.lockBudget(jobId, provider, maxCost, deadline);

        bool failed = false;
        try settlement.lockBudget(jobId, provider, maxCost, deadline) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "Double locking same job id must revert");
    }

    function test_escrow_refund_after_deadline() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-003"));
        uint256 maxCost = 50 * 10 ** 18;
        uint256 deadline = block.timestamp;

        token.approve(address(settlement), maxCost);
        settlement.lockBudget(jobId, provider, maxCost, deadline);

        uint256 balBefore = token.balanceOf(address(this));
        // Owner refund after deadline — no voucher required, so nonce/expiry are ignored.
        settlement.refundJob(address(this), jobId, 0, 0, "");
        require(token.balanceOf(address(this)) == balBefore + maxCost, "Refund amount mismatch");
    }

    // --- settleJob coverage (audit #1): guards, signature rejection, pause ---

    function _lock(bytes32 jobId, uint256 maxCost, uint256 deadline) internal {
        token.approve(address(settlement), maxCost);
        settlement.lockBudget(jobId, provider, maxCost, deadline);
    }

    function _future() internal view returns (uint256) {
        return block.timestamp + ONE_HOUR;
    }

    function test_settle_reverts_cost_exceeds_budget() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-settle-1"));
        _lock(jobId, 50 * 10 ** 18, block.timestamp + ONE_HOUR);
        bool failed = false;
        try settlement.settleJob(address(this), jobId, 60 * 10 ** 18, 1, 0, _future(), "0x") {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "settle must revert when finalCost exceeds locked budget");
    }

    function test_settle_reverts_fee_exceeds_cost() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-settle-2"));
        _lock(jobId, 50 * 10 ** 18, block.timestamp + ONE_HOUR);
        bool failed = false;
        try settlement.settleJob(address(this), jobId, 40 * 10 ** 18, 50 * 10 ** 18, 0, _future(), "0x") {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "settle must revert when fee exceeds final cost");
    }

    function test_settle_reverts_on_invalid_signature() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-settle-3"));
        _lock(jobId, 50 * 10 ** 18, block.timestamp + ONE_HOUR);
        bytes memory badSig = new bytes(65);
        bool failed = false;
        try settlement.settleJob(address(this), jobId, 40 * 10 ** 18, 4 * 10 ** 18, 0, _future(), badSig) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "settle must revert on an invalid voucher signature");
    }

    function test_settle_reverts_when_paused() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-settle-4"));
        _lock(jobId, 50 * 10 ** 18, block.timestamp + ONE_HOUR);
        settlement.pause();
        bool failed = false;
        try settlement.settleJob(address(this), jobId, 40 * 10 ** 18, 4 * 10 ** 18, 0, _future(), "0x") {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "settle must revert while paused");
    }

    function test_refund_works_while_paused() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-settle-5"));
        uint256 maxCost = 50 * 10 ** 18;
        _lock(jobId, maxCost, block.timestamp);
        settlement.pause();
        uint256 balBefore = token.balanceOf(address(this));
        settlement.refundJob(address(this), jobId, 0, 0, "");
        require(
            token.balanceOf(address(this)) == balBefore + maxCost,
            "refund must succeed even while paused"
        );
    }

    // --- A3: On-chain jobId squatting protection test ---
    function test_non_owner_cannot_block_job_escrow() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-squat-target-123"));
        uint256 maxCost = 25 * 10 ** 18;
        EscrowSquatterHelper squatter = new EscrowSquatterHelper();
        token.transfer(address(squatter), maxCost);

        // 1. Attacker attempts to front-run/squat the same jobId first
        squatter.squat(settlement, token, jobId, provider, maxCost);

        // 2. Legitimate customer locks the same jobId -> SUCCEEDS because escrows are keyed by (customer, jobId)!
        token.approve(address(settlement), maxCost);
        settlement.lockBudget(jobId, provider, maxCost, block.timestamp + ONE_HOUR);

        // Verify legitimate customer's escrow is registered
        (address c, , uint256 amt, , bool settled, ) = settlement.getEscrow(address(this), jobId);
        require(c == address(this), "Customer mismatch");
        require(amt == maxCost, "Amount mismatch");
        require(!settled, "Should not be settled");

        // Verify squatter's escrow is also distinct and tracked separately
        (address sc, , uint256 samt, , , ) = settlement.getEscrow(address(squatter), jobId);
        require(sc == address(squatter), "Squatter customer mismatch");
        require(samt == maxCost, "Squatter amount mismatch");

        // 3. Customer trying to double-lock their own escrow STILL reverts
        bool failed = false;
        try settlement.lockBudget(jobId, provider, maxCost, block.timestamp + ONE_HOUR) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "Double locking same job id by same customer must revert");
    }

    // =====================================================================
    // D1 hardening: voucher nonce + expiry, signer binding, conservation
    // =====================================================================

    function _useGoodSigner() internal {
        settlement.setPlatformSigner(address(goodSigner));
    }

    /// @dev Happy path with an accepted voucher: every wei is accounted for.
    function test_settle_with_valid_voucher_splits_funds_exactly() public {
        _useGoodSigner();
        bytes32 jobId = keccak256(abi.encodePacked("job-ok-1"));
        uint256 locked = 100 * 10 ** 18;
        uint256 finalCost = 60 * 10 ** 18;
        uint256 fee = 6 * 10 ** 18;
        _lock(jobId, locked, _future());

        uint256 selfBefore = token.balanceOf(address(this));
        settlement.settleJob(address(this), jobId, finalCost, fee, 0, _future(), ANY_SIG);

        require(token.balanceOf(provider) == finalCost - fee, "provider payout mismatch");
        require(token.balanceOf(feeTreasury) == fee, "platform fee mismatch");
        require(
            token.balanceOf(address(this)) == selfBefore + (locked - finalCost),
            "customer refund mismatch"
        );
        require(token.balanceOf(address(settlement)) == 0, "escrow must be emptied");

        (, , , , bool settled, uint256 nonce) = settlement.getEscrow(address(this), jobId);
        require(settled, "escrow must be marked settled");
        require(nonce == 1, "consumed voucher must advance the nonce");
    }

    function test_settle_twice_reverts() public {
        _useGoodSigner();
        bytes32 jobId = keccak256(abi.encodePacked("job-twice"));
        _lock(jobId, 100 * 10 ** 18, _future());
        settlement.settleJob(address(this), jobId, 50 * 10 ** 18, 5 * 10 ** 18, 0, _future(), ANY_SIG);

        bool failed = false;
        // Replay at the advanced nonce — still rejected because the escrow is closed.
        try settlement.settleJob(address(this), jobId, 50 * 10 ** 18, 5 * 10 ** 18, 1, _future(), ANY_SIG) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "settling the same escrow twice must revert");
    }

    function test_refund_after_settle_reverts() public {
        _useGoodSigner();
        bytes32 jobId = keccak256(abi.encodePacked("job-refund-after-settle"));
        _lock(jobId, 100 * 10 ** 18, block.timestamp); // deadline already reached
        settlement.settleJob(address(this), jobId, 50 * 10 ** 18, 5 * 10 ** 18, 0, _future(), ANY_SIG);

        bool failed = false;
        try settlement.refundJob(address(this), jobId, 1, _future(), ANY_SIG) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "refunding an already-settled escrow must revert (double spend)");
        require(token.balanceOf(address(settlement)) == 0, "no extra tokens may leave escrow");
    }

    function test_settle_reverts_on_expired_voucher() public {
        _useGoodSigner();
        bytes32 jobId = keccak256(abi.encodePacked("job-expired"));
        _lock(jobId, 100 * 10 ** 18, _future());

        bool failed = false;
        // expiry strictly in the past; the signer itself would accept, so only the deadline check
        // can be what rejects this.
        try settlement.settleJob(
            address(this), jobId, 50 * 10 ** 18, 5 * 10 ** 18, 0, block.timestamp - 1, ANY_SIG
        ) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "an expired settlement voucher must revert");

        // ...and the identical voucher with a live expiry succeeds, proving expiry was the cause.
        settlement.settleJob(address(this), jobId, 50 * 10 ** 18, 5 * 10 ** 18, 0, _future(), ANY_SIG);
    }

    function test_settle_reverts_on_stale_nonce_after_invalidation() public {
        _useGoodSigner();
        bytes32 jobId = keccak256(abi.encodePacked("job-stale-nonce"));
        _lock(jobId, 100 * 10 ** 18, _future());

        // Platform retracts the voucher it signed at nonce 0 (e.g. wrong billing data).
        settlement.invalidateVouchers(address(this), jobId);

        bool failed = false;
        try settlement.settleJob(address(this), jobId, 50 * 10 ** 18, 5 * 10 ** 18, 0, _future(), ANY_SIG) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "a voucher at a retracted nonce must revert");

        // The reissued voucher at the current nonce works.
        settlement.settleJob(address(this), jobId, 50 * 10 ** 18, 5 * 10 ** 18, 1, _future(), ANY_SIG);
        require(token.balanceOf(address(settlement)) == 0, "escrow must be emptied once");
    }

    function test_early_refund_voucher_nonce_and_expiry_enforced() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-early-refund"));
        uint256 locked = 30 * 10 ** 18;
        _lock(jobId, locked, _future()); // deadline NOT reached -> voucher required

        // No voucher at all before the deadline, against the real EOA signer.
        bool failed = false;
        try settlement.refundJob(address(this), jobId, 0, _future(), "") {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "early refund without a voucher must revert");

        _useGoodSigner();

        // Expired refund voucher.
        failed = false;
        try settlement.refundJob(address(this), jobId, 0, block.timestamp - 1, ANY_SIG) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "expired early-refund voucher must revert");

        // Stale nonce refund voucher.
        failed = false;
        try settlement.refundJob(address(this), jobId, 7, _future(), ANY_SIG) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "early-refund voucher at the wrong nonce must revert");

        uint256 before = token.balanceOf(address(this));
        settlement.refundJob(address(this), jobId, 0, _future(), ANY_SIG);
        require(token.balanceOf(address(this)) == before + locked, "early refund must return full lock");
    }

    function test_settle_reverts_when_signer_rejects() public {
        settlement.setPlatformSigner(address(badSigner));
        bytes32 jobId = keccak256(abi.encodePacked("job-wrong-signer"));
        _lock(jobId, 100 * 10 ** 18, _future());

        bool failed = false;
        try settlement.settleJob(address(this), jobId, 50 * 10 ** 18, 5 * 10 ** 18, 0, _future(), ANY_SIG) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "a voucher the configured signer rejects must revert");

        // Rotating to the accepting signer makes the very same call succeed.
        _useGoodSigner();
        settlement.settleJob(address(this), jobId, 50 * 10 ** 18, 5 * 10 ** 18, 0, _future(), ANY_SIG);
    }

    function test_owner_cannot_seize_escrowed_funds() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-no-seize"));
        uint256 locked = 40 * 10 ** 18;
        CustomerWallet wallet = new CustomerWallet();
        token.transfer(address(wallet), locked);
        wallet.lock(settlement, token, jobId, provider, locked, block.timestamp);

        uint256 ownerBefore = token.balanceOf(address(this));
        // Owner may *trigger* the post-deadline refund on the customer's behalf...
        settlement.refundJob(address(wallet), jobId, 0, 0, "");
        // ...but the tokens land on the customer, not on the owner.
        require(token.balanceOf(address(wallet)) == locked, "refund must pay the recorded customer");
        require(token.balanceOf(address(this)) == ownerBefore, "owner must not receive escrowed funds");
        require(token.balanceOf(address(settlement)) == 0, "escrow drained to customer only");
    }

    // ---------------------------------------------------------------------
    // Invariant / fuzz-style harness (deterministic loops, no cheatcodes)
    // ---------------------------------------------------------------------

    /**
     * @notice Token conservation over a mixed sequence of settle / refund / still-locked escrows.
     * @dev For any sequence of operations the contract must satisfy
     *      sum(locked) == sum(providerPayouts) + sum(platformFees) + sum(customerRefunds)
     *                     + sum(stillLocked)
     *      and the settlement contract must hold exactly the still-locked amount. The loop sweeps
     *      cost/fee ratios including the degenerate 0% and 100% ends.
     */
    function test_invariant_token_conservation_mixed_paths() public {
        _useGoodSigner();

        uint256 sumLocked;
        uint256 sumPayouts;
        uint256 sumFees;
        uint256 sumRefunds;
        uint256 sumStillLocked;

        uint256 providerBefore = token.balanceOf(provider);
        uint256 treasuryBefore = token.balanceOf(feeTreasury);
        uint256 selfBefore = token.balanceOf(address(this));

        for (uint256 i = 0; i < 24; i++) {
            bytes32 jobId = keccak256(abi.encodePacked("inv-mixed", i));
            uint256 locked = (i + 1) * 10 ** 18;
            uint256 finalCost = (locked * (i % 5)) / 4; // 0%, 25%, 50%, 75%, 100% of the lock
            // 0%, 10%, 20% of the cost — every value admissible under MAX_FEE_BPS.
            uint256 fee = (finalCost * (i % 3)) / 10;
            uint256 path = i % 3;

            sumLocked += locked;

            if (path == 0) {
                _lock(jobId, locked, _future());
                settlement.settleJob(address(this), jobId, finalCost, fee, 0, _future(), ANY_SIG);
                sumPayouts += finalCost - fee;
                sumFees += fee;
                sumRefunds += locked - finalCost;
            } else if (path == 1) {
                _lock(jobId, locked, block.timestamp); // immediately refundable
                settlement.refundJob(address(this), jobId, 0, 0, "");
                sumRefunds += locked;
            } else {
                _lock(jobId, locked, _future());
                sumStillLocked += locked;
            }

            // Per-step invariant: nothing has leaked at any point in the sequence.
            require(
                token.balanceOf(address(settlement)) == sumStillLocked,
                "escrow balance must equal still-locked total at every step"
            );
        }

        require(
            sumLocked == sumPayouts + sumFees + sumRefunds + sumStillLocked,
            "token conservation violated"
        );
        require(token.balanceOf(provider) - providerBefore == sumPayouts, "provider total mismatch");
        require(token.balanceOf(feeTreasury) - treasuryBefore == sumFees, "treasury total mismatch");
        require(
            selfBefore - token.balanceOf(address(this)) == sumLocked - sumRefunds,
            "customer net outflow mismatch"
        );
    }

    /**
     * @notice Per-escrow conservation across a dense sweep of (cost, fee) combinations.
     * @dev Every settlement must split the lock into exactly three parts with no remainder and no
     *      creation of tokens, for every admissible cost/fee pair including the boundaries
     *      finalCost == locked and fee == MAX_FEE_BPS of finalCost.
     */
    function test_invariant_settlement_split_is_exact_for_all_ratios() public {
        _useGoodSigner();
        uint256 n;

        for (uint256 c = 0; c <= 4; c++) {
            for (uint256 f = 0; f <= 3; f++) {
                bytes32 jobId = keccak256(abi.encodePacked("inv-split", n));
                uint256 locked = 100 * 10 ** 18 + n;
                uint256 finalCost = (locked * c) / 4;
                // 0%, 10%, 20%, 30% — the full admissible range up to MAX_FEE_BPS.
                uint256 fee = (finalCost * f) / 10;

                _lock(jobId, locked, _future());

                uint256 providerBefore = token.balanceOf(provider);
                uint256 treasuryBefore = token.balanceOf(feeTreasury);
                uint256 selfBefore = token.balanceOf(address(this));
                uint256 escrowBefore = token.balanceOf(address(settlement));

                settlement.settleJob(address(this), jobId, finalCost, fee, 0, _future(), ANY_SIG);

                uint256 payout = token.balanceOf(provider) - providerBefore;
                uint256 paidFee = token.balanceOf(feeTreasury) - treasuryBefore;
                uint256 refund = token.balanceOf(address(this)) - selfBefore;

                require(payout == finalCost - fee, "payout must be cost minus fee");
                require(paidFee == fee, "fee must be paid exactly");
                require(payout + paidFee + refund == locked, "split must exhaust the lock exactly");
                require(
                    escrowBefore - token.balanceOf(address(settlement)) == locked,
                    "escrow must release exactly the locked amount"
                );
                n++;
            }
        }
        require(n == 20, "sweep must cover every ratio combination");
    }

    /**
     * @notice No admissible voucher can ever pull more than the lock out of the contract.
     * @dev Sweeps costs above the lock and fees above the cost; each must revert and leave the
     *      escrow untouched, so an over-budget voucher cannot drain other customers' escrows.
     */
    /**
     * @notice The owner cannot rotate its way into an escrow's principal.
     * @dev Exercises the exact seizure sequence the header invariant claims is impossible:
     *      rotate the fee treasury to the attacker, rotate the platform signer to a key the
     *      attacker controls, then present a voucher with platformFee == finalCost ==
     *      lockedAmount. {MAX_FEE_BPS} must reject it, and the escrow must still be intact.
     */
    function test_owner_cannot_drain_escrow_via_signer_and_treasury_rotation() public {
        _useGoodSigner();
        bytes32 jobId = keccak256(abi.encodePacked("job-fee-seizure"));
        uint256 locked = 100 * 10 ** 18;
        _lock(jobId, locked, _future());

        // (1) + (2) the owner rotates both owner-controlled addresses.
        address attacker = address(0xBADBAD);
        settlement.setFeeTreasury(attacker);
        settlement.setPlatformSigner(address(goodSigner)); // accepts ANY signature

        uint256 attackerBefore = token.balanceOf(attacker);
        uint256 escrowBefore = token.balanceOf(address(settlement));

        // (3) a 100%-fee voucher: providerPayout = 0, customerRefund = 0.
        bool failed = false;
        try settlement.settleJob(address(this), jobId, locked, locked, 0, _future(), ANY_SIG) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "a 100% platform fee must be rejected by MAX_FEE_BPS");
        require(token.balanceOf(attacker) == attackerBefore, "no tokens may reach the treasury");
        require(token.balanceOf(address(settlement)) == escrowBefore, "escrow must be untouched");

        // The capped fee is still honored, so the cap is a bound and not a ban.
        uint256 cappedFee = (locked * settlement.MAX_FEE_BPS()) / 10_000;
        settlement.settleJob(address(this), jobId, locked, cappedFee, 0, _future(), ANY_SIG);
        require(token.balanceOf(attacker) == attackerBefore + cappedFee, "capped fee must pay out");
        require(token.balanceOf(provider) >= locked - cappedFee, "provider keeps the remainder");
    }

    function test_settle_reverts_when_fee_exceeds_the_bps_cap() public {
        _useGoodSigner();
        bytes32 jobId = keccak256(abi.encodePacked("job-fee-cap"));
        uint256 locked = 100 * 10 ** 18;
        _lock(jobId, locked, _future());

        uint256 overCap = (locked * (settlement.MAX_FEE_BPS() + 1)) / 10_000;
        bool failed = false;
        try settlement.settleJob(address(this), jobId, locked, overCap, 0, _future(), ANY_SIG) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "one bp above the cap must revert");
    }

    function test_invariant_overdraw_vouchers_always_revert() public {
        _useGoodSigner();
        uint256 locked = 50 * 10 ** 18;

        for (uint256 i = 1; i <= 6; i++) {
            bytes32 jobId = keccak256(abi.encodePacked("inv-overdraw", i));
            _lock(jobId, locked, _future());
            uint256 escrowBefore = token.balanceOf(address(settlement));

            bool failed = false;
            try settlement.settleJob(address(this), jobId, locked + i, 0, 0, _future(), ANY_SIG) {
                failed = false;
            } catch {
                failed = true;
            }
            require(failed, "finalCost above the lock must revert");

            failed = false;
            try settlement.settleJob(address(this), jobId, locked / 2, locked / 2 + i, 0, _future(), ANY_SIG) {
                failed = false;
            } catch {
                failed = true;
            }
            require(failed, "fee above finalCost must revert");

            require(
                token.balanceOf(address(settlement)) == escrowBefore,
                "a reverted settlement must not move tokens"
            );
        }
    }
}
