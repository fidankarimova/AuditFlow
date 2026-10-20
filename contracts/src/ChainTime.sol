// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Current time in seconds on any target chain. EVM chains report block.timestamp in seconds;
///         FISCO BCOS reports it in milliseconds. A seconds value above 1e11 would be year 5138, so anything
///         larger is treated as milliseconds.
library ChainTime {
    uint256 internal constant MS_THRESHOLD = 1e11;

    function nowSeconds() internal view returns (uint256) {
        uint256 t = block.timestamp;
        return t > MS_THRESHOLD ? t / 1000 : t;
    }
}
