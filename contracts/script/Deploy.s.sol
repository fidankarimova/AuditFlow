// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {InvoiceRegistry} from "../src/InvoiceRegistry.sol";
import {AgreementLedger} from "../src/AgreementLedger.sol";

/// @notice Deploys InvoiceRegistry + AgreementLedger and wires roles.
///         Only the admin needs faucet ETH: it forwards gas money to the coordinator and supplier.
///         Buyer confirmations and human approvals are EIP-712 signatures relayed by the coordinator.
contract Deploy is Script {
    uint256 internal constant COORDINATOR_GAS = 0.00004 ether;
    uint256 internal constant SUPPLIER_GAS = 0.000005 ether;

    function run() external returns (InvoiceRegistry registry, AgreementLedger ledger) {
        uint256 adminKey = vm.envUint("ADMIN_PRIVATE_KEY");
        uint256 supplierKey = vm.envUint("SUPPLIER_SIGNER_KEY");
        address admin = vm.addr(adminKey);
        address coordinator = vm.addr(vm.envUint("COORDINATOR_PRIVATE_KEY"));
        address supplier = vm.addr(supplierKey);

        vm.startBroadcast(adminKey);
        registry = new InvoiceRegistry(admin);
        ledger = new AgreementLedger(admin, registry);
        registry.grantRole(registry.SETTLER_ROLE(), address(ledger));
        registry.grantRole(registry.REGISTRAR_ROLE(), coordinator);
        ledger.grantRole(ledger.COORDINATOR_ROLE(), coordinator);
        // FISCO BCOS has no gas fees, so there is nothing to forward there (FUND_GAS=false).
        if (vm.envOr("FUND_GAS", true)) {
            if (coordinator.balance < COORDINATOR_GAS) payable(coordinator).transfer(COORDINATOR_GAS);
            if (supplier.balance < SUPPLIER_GAS) payable(supplier).transfer(SUPPLIER_GAS);
        }
        vm.stopBroadcast();

        // Supplier (seller) authorizes the coordinator to register its invoices.
        vm.startBroadcast(supplierKey);
        registry.setAuthorizedRegistrar(coordinator, true);
        vm.stopBroadcast();

        console.log("InvoiceRegistry", address(registry));
        console.log("AgreementLedger", address(ledger));
    }
}
