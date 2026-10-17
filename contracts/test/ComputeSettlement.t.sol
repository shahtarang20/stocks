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

contract ComputeSettlementTest {
    ComputeCoinToken public token;
    ComputeSettlement public settlement;

    address public owner;
    address public platformSigner;
    uint256 public platformSignerPrivateKey;
    address public feeTreasury;
    address public customer;
    address public provider;

    uint256 public constant ONE_HOUR = 3600;

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

        (address c, address p, uint256 amt, uint256 dl, bool settled) = settlement.getEscrow(address(this), jobId);
        require(c == address(this), "Customer mismatch");
        require(p == provider, "Provider mismatch");
        require(amt == maxCost, "Amount mismatch");
        require(dl == deadline, "Deadline mismatch");
        require(!settled, "Must not be settled");
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
        // Owner refund after deadline
        settlement.refundJob(address(this), jobId, "");
        require(token.balanceOf(address(this)) == balBefore + maxCost, "Refund amount mismatch");
    }

    // --- settleJob coverage (audit #1): guards, signature rejection, pause ---

    function _lock(bytes32 jobId, uint256 maxCost, uint256 deadline) internal {
        token.approve(address(settlement), maxCost);
        settlement.lockBudget(jobId, provider, maxCost, deadline);
    }

    function test_settle_reverts_cost_exceeds_budget() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-settle-1"));
        _lock(jobId, 50 * 10 ** 18, block.timestamp + ONE_HOUR);
        bool failed = false;
        try settlement.settleJob(address(this), jobId, 60 * 10 ** 18, 1, "0x") {
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
        try settlement.settleJob(address(this), jobId, 40 * 10 ** 18, 50 * 10 ** 18, "0x") {
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
        try settlement.settleJob(address(this), jobId, 40 * 10 ** 18, 4 * 10 ** 18, badSig) {
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
        try settlement.settleJob(address(this), jobId, 40 * 10 ** 18, 4 * 10 ** 18, "0x") {
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
        settlement.refundJob(address(this), jobId, "");
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
        (address c, , uint256 amt, , bool settled) = settlement.getEscrow(address(this), jobId);
        require(c == address(this), "Customer mismatch");
        require(amt == maxCost, "Amount mismatch");
        require(!settled, "Should not be settled");

        // Verify squatter's escrow is also distinct and tracked separately
        (address sc, , uint256 samt, , ) = settlement.getEscrow(address(squatter), jobId);
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
}
