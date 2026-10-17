// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Burnable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import {ERC20Pausable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Pausable.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/**
 * @title ComputeCoinToken (COMPUTE)
 * @notice The native utility and settlement token for the ComputeCoin GPU compute network.
 * @dev Implements standard ERC-20 with EIP-2612 permit, burnability, and pausable safeguards.
 */
contract ComputeCoinToken is ERC20, ERC20Burnable, ERC20Pausable, ERC20Permit, Ownable {
    uint256 public constant MAX_SUPPLY = 1_000_000_000 * 10 ** 18; // 1 Billion COMPUTE

    event TokensMinted(address indexed to, uint256 amount);

    /**
     * @param initialOwner Address of the platform treasury / multisig receiving the genesis allocation.
     */
    constructor(address initialOwner)
        ERC20("ComputeCoin", "COMPUTE")
        ERC20Permit("ComputeCoin")
        Ownable(initialOwner)
    {
        require(initialOwner != address(0), "Invalid initial owner");
        _mint(initialOwner, MAX_SUPPLY);
        emit TokensMinted(initialOwner, MAX_SUPPLY);
    }

    /**
     * @notice Emergency circuit breaker to pause token transfers in case of vulnerability.
     */
    function pause() external onlyOwner {
        _pause();
    }

    /**
     * @notice Unpause token transfers once resolved.
     */
    function unpause() external onlyOwner {
        _unpause();
    }

    /**
     * @dev Hook that is called before any transfer of tokens. Handled by ERC20Pausable.
     */
    function _update(address from, address to, uint256 value)
        internal
        override(ERC20, ERC20Pausable)
    {
        super._update(from, to, value);
    }
}
