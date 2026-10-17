// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ComputeCoinToken} from "../src/ComputeCoinToken.sol";
import {ComputeSettlement} from "../src/ComputeSettlement.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

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
        // Public address for private key 0xA11CE
        // Derived or use an address and mock
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

        (address c, address p, uint256 amt, uint256 dl, bool settled) = settlement.escrows(jobId);
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
        settlement.refundJob(jobId, "");
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
        try settlement.settleJob(jobId, 60 * 10 ** 18, 1, "0x") {
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
        try settlement.settleJob(jobId, 40 * 10 ** 18, 50 * 10 ** 18, "0x") {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "settle must revert when fee exceeds final cost");
    }

    function test_settle_reverts_on_invalid_signature() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-settle-3"));
        _lock(jobId, 50 * 10 ** 18, block.timestamp + ONE_HOUR);
        // Valid amounts but a bogus 65-byte signature -> ECDSA recover != signer.
        bytes memory badSig = new bytes(65);
        bool failed = false;
        try settlement.settleJob(jobId, 40 * 10 ** 18, 4 * 10 ** 18, badSig) {
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
        try settlement.settleJob(jobId, 40 * 10 ** 18, 4 * 10 ** 18, "0x") {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "settle must revert while paused");
    }

    // Audit fix #2: a pause must NOT freeze the customer's post-deadline refund.
    function test_refund_works_while_paused() public {
        bytes32 jobId = keccak256(abi.encodePacked("job-settle-5"));
        uint256 maxCost = 50 * 10 ** 18;
        _lock(jobId, maxCost, block.timestamp); // deadline = now
        settlement.pause();
        uint256 balBefore = token.balanceOf(address(this));
        settlement.refundJob(jobId, ""); // owner/customer self-refund after deadline
        require(
            token.balanceOf(address(this)) == balBefore + maxCost,
            "refund must succeed even while paused"
        );
    }
}
