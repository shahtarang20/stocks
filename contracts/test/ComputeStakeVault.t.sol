// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ComputeCoinToken} from "../src/ComputeCoinToken.sol";
import {ComputeStakeVault} from "../src/ComputeStakeVault.sol";

interface Vm {
    function warp(uint256) external;
}

/// @dev Acts as a provider (or an attacker) so calls come from a distinct address.
contract Actor {
    function deposit(ComputeStakeVault vault, ComputeCoinToken token, bytes32 id, uint256 amount) external {
        token.approve(address(vault), amount);
        vault.depositStake(id, amount);
    }

    function requestUnstake(ComputeStakeVault vault, bytes32 id, uint256 amount) external {
        vault.requestUnstake(id, amount);
    }

    function withdraw(ComputeStakeVault vault, bytes32 id) external {
        vault.withdrawStake(id);
    }

    function proposeSlash(ComputeStakeVault vault, bytes32 id, address to, uint256 comp, uint256 amount) external {
        vault.proposeSlash(id, to, comp, amount, "attack");
    }
}

contract ComputeStakeVaultTest {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    ComputeCoinToken private token;
    ComputeStakeVault private vault;
    Actor private provider;
    Actor private stranger;

    address private insurancePool = address(0x1111);
    address private slasher = address(0x2222);
    address private customer = address(0x3333);
    bytes32 private machineA = keccak256("machine-a");
    uint256 private constant E = 10 ** 18;

    function setUp() public {
        token = new ComputeCoinToken(address(this));
        vault = new ComputeStakeVault(address(token), address(this), insurancePool, slasher);
        provider = new Actor();
        stranger = new Actor();
        token.transfer(address(provider), 20_000 * E);
        token.transfer(address(stranger), 20_000 * E);
        vault.registerMachine(machineA, address(provider));
    }

    function test_deposit_enables_sealed_and_sealed_pro_thresholds() public {
        require(!vault.isEligibleForTier(machineA, ComputeStakeVault.Tier.Sealed), "eligible before deposit");
        provider.deposit(vault, token, machineA, 1_500 * E);
        require(vault.isEligibleForTier(machineA, ComputeStakeVault.Tier.Sealed), "not eligible for Sealed");
        require(!vault.isEligibleForTier(machineA, ComputeStakeVault.Tier.SealedPro), "eligible for Sealed Pro too early");
        provider.deposit(vault, token, machineA, 4_000 * E);
        require(vault.isEligibleForTier(machineA, ComputeStakeVault.Tier.SealedPro), "not eligible for Sealed Pro");
    }

    function test_nobody_can_squat_an_unregistered_or_foreign_machine_id() public {
        bytes32 unregistered = keccak256("machine-b");
        try stranger.deposit(vault, token, unregistered, 1_000 * E) {
            revert("deposit to an unregistered machine id was accepted");
        } catch {}
        try stranger.deposit(vault, token, machineA, 1_000 * E) {
            revert("stranger staked on another provider's machine");
        } catch {}
    }

    function test_slash_is_delayed_freezes_the_stake_and_pays_everyone() public {
        provider.deposit(vault, token, machineA, 2_000 * E);
        vault.proposeSlash(machineA, customer, 800 * E, 1_500 * E, "CANARY_LEAK");

        // Frozen: the host cannot run away with the stake while the slash is pending.
        try provider.requestUnstake(vault, machineA, 100 * E) {
            revert("unstake allowed during a pending slash");
        } catch {}
        require(!vault.isEligibleForTier(machineA, ComputeStakeVault.Tier.Sealed), "eligible while a slash is pending");

        // Not executable before the delay.
        try vault.executeSlash(machineA) {
            revert("slash executed before the delay");
        } catch {}

        vm.warp(block.timestamp + 2 days);
        uint256 customerBefore = token.balanceOf(customer);
        uint256 poolBefore = token.balanceOf(insurancePool);
        vault.executeSlash(machineA);
        require(token.balanceOf(customer) == customerBefore + 800 * E, "customer not compensated");
        require(token.balanceOf(insurancePool) == poolBefore + 700 * E, "insurance pool not paid");

        (, uint256 bonded, uint256 unbonding,, bool banned) = vault.stakes(machineA);
        require(banned, "machine not banned");
        require(bonded == 0 && unbonding == 500 * E, "unslashed remainder should be unbonding, not locked");

        // The remainder is the provider's after the unbonding period, and not before.
        try provider.withdraw(vault, machineA) {
            revert("withdrew before unbonding elapsed");
        } catch {}
        vm.warp(block.timestamp + 7 days);
        uint256 provBefore = token.balanceOf(address(provider));
        provider.withdraw(vault, machineA);
        require(token.balanceOf(address(provider)) == provBefore + 500 * E, "remainder not returned");
        require(token.balanceOf(address(vault)) == 0, "tokens stuck in the vault");
    }

    function test_owner_can_cancel_a_slash_on_appeal() public {
        provider.deposit(vault, token, machineA, 2_000 * E);
        vault.proposeSlash(machineA, customer, 0, 2_000 * E, "DISPUTED");
        vault.cancelSlash(machineA);
        vm.warp(block.timestamp + 3 days);
        try vault.executeSlash(machineA) {
            revert("cancelled slash executed");
        } catch {}
        provider.requestUnstake(vault, machineA, 2_000 * E); // unfrozen again
    }

    function test_compensation_without_a_recipient_is_refused() public {
        provider.deposit(vault, token, machineA, 2_000 * E);
        try vault.proposeSlash(machineA, address(0), 100 * E, 1_000 * E, "x") {
            revert("compensation to address(0) accepted; tokens would be stuck");
        } catch {}
    }

    function test_only_slasher_or_owner_can_propose() public {
        provider.deposit(vault, token, machineA, 2_000 * E);
        try stranger.proposeSlash(vault, machineA, address(stranger), 1_000 * E, 1_000 * E) {
            revert("stranger proposed a slash");
        } catch {}
    }

    function test_unbonding_period_has_a_floor_and_outlasts_the_slash_delay() public {
        try vault.setUnbondingPeriod(0) {
            revert("unbonding period of zero accepted");
        } catch {}
        try vault.setUnbondingPeriod(2 days) {
            revert("unbonding period not longer than the slash delay accepted");
        } catch {}
        vault.setUnbondingPeriod(10 days);
        try vault.setSlashDelay(10 days) {
            revert("slash delay equal to unbonding accepted");
        } catch {}
    }

    function test_merkle_root_history_cannot_be_rewritten() public {
        bytes32 r1 = keccak256("root-42");
        vault.commitMerkleRoot(r1, 42);
        require(vault.latestMerkleRoot() == r1 && vault.totalAnchoredLeaves() == 42, "anchor not stored");
        vault.commitMerkleRoot(r1, 42); // idempotent re-anchor is fine
        try vault.commitMerkleRoot(keccak256("forged-root-42"), 42) {
            revert("a different root was accepted for the same size");
        } catch {}
        try vault.commitMerkleRoot(keccak256("root-41"), 41) {
            revert("the log was allowed to shrink");
        } catch {}
        vault.commitMerkleRoot(keccak256("root-43"), 43);
    }
}
