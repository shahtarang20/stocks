// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {VestingWallet} from "@openzeppelin/contracts/finance/VestingWallet.sol";
import {VestingWalletCliff} from "@openzeppelin/contracts/finance/VestingWalletCliff.sol";

/**
 * @title ComputeTimelock
 * @notice Admin actions on the token / settlement contracts go through this timelock, so any change is
 *         visible on-chain for `minDelay` seconds before it can run.
 * @dev A thin, unmodified OpenZeppelin TimelockController. Deploy with `admin = address(0)` so that the
 *      timelock administers itself and nobody keeps a backdoor to skip the delay.
 */
contract ComputeTimelock is TimelockController {
    constructor(uint256 minDelay, address[] memory proposers, address[] memory executors, address admin)
        TimelockController(minDelay, proposers, executors, admin)
    {}
}

/**
 * @title ComputeVesting
 * @notice Holds a beneficiary's allocation and releases it linearly over `durationSeconds`, with nothing
 *         releasable before `cliffSeconds` have passed since `startTimestamp`.
 * @dev A thin, unmodified OpenZeppelin VestingWalletCliff. Anyone may call `release(token)`; the funds
 *      always go to the beneficiary.
 */
contract ComputeVesting is VestingWalletCliff {
    constructor(address beneficiary, uint64 startTimestamp, uint64 durationSeconds, uint64 cliffSeconds)
        VestingWallet(beneficiary, startTimestamp, durationSeconds)
        VestingWalletCliff(cliffSeconds)
    {}
}
