// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BaseTest} from "./Base.t.sol";
import {InvoiceRegistry} from "../src/InvoiceRegistry.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";

contract InvoiceRegistryTest is BaseTest {
    function test_RegisterAndConfirm() public {
        uint256 id = _register(FACE);
        assertEq(uint8(registry.statusOf(id)), uint8(InvoiceRegistry.Status.Registered));
        vm.prank(buyer);
        registry.confirmByBuyer(id);
        assertEq(uint8(registry.statusOf(id)), uint8(InvoiceRegistry.Status.Confirmed));
    }

    function test_MillisecondTimestampsLikeFiscoBcos() public {
        // FISCO BCOS reports block.timestamp in milliseconds; due dates stay in seconds.
        vm.warp(block.timestamp * 1000);
        uint256 id = _register(FACE);
        assertEq(uint8(registry.statusOf(id)), uint8(InvoiceRegistry.Status.Registered));
        bytes32 h = _invoiceHash("PAST-1", FACE, uint64(block.timestamp / 1000 - 1));
        vm.prank(supplier);
        vm.expectRevert(InvoiceRegistry.InvalidInvoice.selector);
        registry.registerInvoice(h, supplier, buyer, FACE, uint64(block.timestamp / 1000 - 1));
    }

    function test_SellerRegistersDirectly() public {
        bytes32 h = _invoiceHash("SELF-1", FACE, dueDate);
        vm.prank(supplier);
        uint256 id = registry.registerInvoice(h, supplier, buyer, FACE, dueDate);
        assertEq(registry.getInvoice(id).seller, supplier);
        assertTrue(registry.hashUsed(h));
    }

    function test_ConfirmWithEIP712Signature() public {
        uint256 id = _register(FACE);

        // Rebuild the digest independently so off-chain signers (Python) can match it.
        bytes32 domain = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256("AuditFlow InvoiceRegistry"),
                keccak256("1"),
                block.chainid,
                address(registry)
            )
        );
        InvoiceRegistry.Invoice memory inv = registry.getInvoice(id);
        bytes32 structHash = keccak256(
            abi.encode(
                keccak256("InvoiceConfirmation(uint256 invoiceId,bytes32 invoiceHash,uint256 faceValue,uint64 dueDate)"),
                id,
                inv.invoiceHash,
                inv.faceValue,
                inv.dueDate
            )
        );
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", domain, structHash));
        assertEq(digest, registry.confirmationDigest(id));
        assertEq(domain, registry.domainSeparator());

        bytes memory sig = _sign(buyerKey, digest);
        vm.prank(stranger); // anyone can relay the buyer's signature
        registry.confirmByBuyerWithSig(id, sig);
        assertEq(uint8(registry.statusOf(id)), uint8(InvoiceRegistry.Status.Confirmed));

        // Replay fails: invoice is no longer Registered
        vm.expectRevert(
            abi.encodeWithSelector(InvoiceRegistry.InvalidStatus.selector, id, InvoiceRegistry.Status.Confirmed)
        );
        registry.confirmByBuyerWithSig(id, sig);
    }

    function test_RevertWhen_DuplicateInvoiceHash() public {
        bytes32 h = _invoiceHash("DUP-1", FACE, dueDate);
        vm.prank(coordinator);
        registry.registerInvoice(h, supplier, buyer, FACE, dueDate);

        vm.prank(coordinator);
        vm.expectRevert(abi.encodeWithSelector(InvoiceRegistry.DuplicateInvoice.selector, h));
        registry.registerInvoice(h, supplier, buyer, FACE, dueDate);

        // Also blocked for the seller directly and with a different buyer address
        vm.prank(supplier);
        vm.expectRevert(abi.encodeWithSelector(InvoiceRegistry.DuplicateInvoice.selector, h));
        registry.registerInvoice(h, supplier, stranger, FACE, dueDate);
    }

    function test_RevertWhen_NonBuyerConfirms() public {
        uint256 id = _register(FACE);
        vm.prank(supplier);
        vm.expectRevert(InvoiceRegistry.NotBuyer.selector);
        registry.confirmByBuyer(id);
        vm.prank(coordinator);
        vm.expectRevert(InvoiceRegistry.NotBuyer.selector);
        registry.confirmByBuyer(id);
    }

    function test_RevertWhen_InvalidBuyerSignature() public {
        uint256 id = _register(FACE);
        uint256 other = _register(FACE);
        bytes memory bySeller = _sign(supplierKey, registry.confirmationDigest(id));
        bytes memory forOtherInvoice = _sign(buyerKey, registry.confirmationDigest(other));

        vm.expectRevert(InvoiceRegistry.InvalidBuyerSignature.selector);
        registry.confirmByBuyerWithSig(id, bySeller);
        vm.expectRevert(InvoiceRegistry.InvalidBuyerSignature.selector);
        registry.confirmByBuyerWithSig(id, hex"deadbeef");
        vm.expectRevert(InvoiceRegistry.InvalidBuyerSignature.selector);
        registry.confirmByBuyerWithSig(id, forOtherInvoice);
    }

    function test_RevertWhen_UnauthorizedRegistration() public {
        bytes32 h = _invoiceHash("X-1", FACE, dueDate);
        vm.prank(stranger);
        vm.expectRevert(InvoiceRegistry.NotAuthorizedToRegister.selector);
        registry.registerInvoice(h, supplier, buyer, FACE, dueDate);

        // Registrar after the seller revoked it
        vm.prank(supplier);
        registry.setAuthorizedRegistrar(coordinator, false);
        vm.prank(coordinator);
        vm.expectRevert(InvoiceRegistry.NotAuthorizedToRegister.selector);
        registry.registerInvoice(h, supplier, buyer, FACE, dueDate);

        // Authorized by the seller but without REGISTRAR_ROLE
        vm.prank(supplier);
        registry.setAuthorizedRegistrar(stranger, true);
        vm.prank(stranger);
        vm.expectRevert(InvoiceRegistry.NotAuthorizedToRegister.selector);
        registry.registerInvoice(h, supplier, buyer, FACE, dueDate);
    }

    function test_RevertWhen_InvalidInvoiceData() public {
        vm.startPrank(supplier);
        vm.expectRevert(InvoiceRegistry.InvalidInvoice.selector);
        registry.registerInvoice(bytes32(0), supplier, buyer, FACE, dueDate);
        vm.expectRevert(InvoiceRegistry.InvalidInvoice.selector);
        registry.registerInvoice(keccak256("a"), supplier, supplier, FACE, dueDate); // self-dealing
        vm.expectRevert(InvoiceRegistry.InvalidInvoice.selector);
        registry.registerInvoice(keccak256("b"), supplier, address(0), FACE, dueDate);
        vm.expectRevert(InvoiceRegistry.InvalidInvoice.selector);
        registry.registerInvoice(keccak256("c"), supplier, buyer, 0, dueDate);
        vm.expectRevert(InvoiceRegistry.InvalidInvoice.selector);
        registry.registerInvoice(keccak256("d"), supplier, buyer, FACE, uint64(block.timestamp));
        vm.stopPrank();
    }

    function test_Cancel() public {
        uint256 id = _registerAndConfirm(FACE);
        vm.prank(supplier);
        registry.cancel(id);
        assertEq(uint8(registry.statusOf(id)), uint8(InvoiceRegistry.Status.Cancelled));
        assertTrue(registry.hashUsed(registry.getInvoice(id).invoiceHash)); // cannot be re-registered
    }

    function test_RevertWhen_NonSellerCancels() public {
        uint256 id = _register(FACE);
        vm.prank(coordinator);
        vm.expectRevert(InvoiceRegistry.NotSeller.selector);
        registry.cancel(id);
    }

    function test_RevertWhen_CancelTwice() public {
        uint256 id = _register(FACE);
        vm.startPrank(supplier);
        registry.cancel(id);
        vm.expectRevert(
            abi.encodeWithSelector(InvoiceRegistry.InvalidStatus.selector, id, InvoiceRegistry.Status.Cancelled)
        );
        registry.cancel(id);
        vm.stopPrank();
    }

    function test_RevertWhen_OutsiderMarksSettled() public {
        uint256 id = _registerAndConfirm(FACE);
        bytes32 role = registry.SETTLER_ROLE();
        vm.prank(coordinator);
        vm.expectRevert(
            abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, coordinator, role)
        );
        registry.markSettled(id);
    }

    function test_RevertWhen_SettleUnconfirmed() public {
        uint256 id = _register(FACE);
        vm.prank(address(ledger));
        vm.expectRevert(
            abi.encodeWithSelector(InvoiceRegistry.InvalidStatus.selector, id, InvoiceRegistry.Status.Registered)
        );
        registry.markSettled(id);
    }
}
