// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {ChainTime} from "./ChainTime.sol";

/// @title InvoiceRegistry
/// @notice Registers invoices by hash (a hash can be used only once) and records the buyer's
///         confirmation, either by a direct call or by an EIP-712 signature relayed by anyone.
///         A confirmed invoice is marked Settled by the AgreementLedger once its early payment settles.
contract InvoiceRegistry is AccessControl, EIP712 {
    /// @notice Off-chain service allowed to register invoices for sellers that authorized it.
    bytes32 public constant REGISTRAR_ROLE = keccak256("REGISTRAR_ROLE");
    /// @notice The AgreementLedger contract.
    bytes32 public constant SETTLER_ROLE = keccak256("SETTLER_ROLE");

    bytes32 public constant CONFIRMATION_TYPEHASH =
        keccak256("InvoiceConfirmation(uint256 invoiceId,bytes32 invoiceHash,uint256 faceValue,uint64 dueDate)");

    enum Status {
        None,
        Registered,
        Confirmed,
        Settled,
        Cancelled
    }

    struct Invoice {
        bytes32 invoiceHash;
        address seller;
        address buyer;
        uint256 faceValue;
        uint64 dueDate;
        Status status;
    }

    uint256 public nextInvoiceId = 1;

    mapping(uint256 => Invoice) private _invoices;
    mapping(bytes32 => bool) public hashUsed;
    mapping(address => mapping(address => bool)) public sellerAuthorizedRegistrar;

    error DuplicateInvoice(bytes32 invoiceHash);
    error NotAuthorizedToRegister();
    error InvalidInvoice();
    error InvalidStatus(uint256 invoiceId, Status status);
    error NotBuyer();
    error NotSeller();
    error InvalidBuyerSignature();

    event RegistrarAuthorizationSet(address indexed seller, address indexed registrar, bool authorized);
    event InvoiceRegistered(
        uint256 indexed invoiceId,
        bytes32 indexed invoiceHash,
        address indexed seller,
        address buyer,
        uint256 faceValue,
        uint64 dueDate,
        address registeredBy
    );
    event InvoiceConfirmed(uint256 indexed invoiceId, address indexed buyer, bool viaSignature);
    event InvoiceSettled(uint256 indexed invoiceId);
    event InvoiceCancelled(uint256 indexed invoiceId);

    constructor(address admin) EIP712("AuditFlow InvoiceRegistry", "1") {
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
    }

    // ----------------------------------------------------------------- seller / registrar

    /// @notice Seller authorizes (or revokes) a registrar to register invoices on its behalf.
    function setAuthorizedRegistrar(address registrar, bool authorized) external {
        sellerAuthorizedRegistrar[msg.sender][registrar] = authorized;
        emit RegistrarAuthorizationSet(msg.sender, registrar, authorized);
    }

    /// @notice Registers an invoice. invoiceHash = keccak256(abi.encode(string sellerTaxId,
    ///         string buyerTaxId, string invoiceNumber, uint256 faceValue, uint64 dueDate)), computed off-chain.
    function registerInvoice(bytes32 invoiceHash, address seller, address buyer, uint256 faceValue, uint64 dueDate)
        external
        returns (uint256 invoiceId)
    {
        bool isSeller = msg.sender == seller;
        bool isRegistrar = hasRole(REGISTRAR_ROLE, msg.sender) && sellerAuthorizedRegistrar[seller][msg.sender];
        if (!isSeller && !isRegistrar) revert NotAuthorizedToRegister();
        if (
            invoiceHash == bytes32(0) || seller == address(0) || buyer == address(0) || buyer == seller
                || faceValue == 0 || dueDate <= ChainTime.nowSeconds()
        ) revert InvalidInvoice();
        if (hashUsed[invoiceHash]) revert DuplicateInvoice(invoiceHash);

        hashUsed[invoiceHash] = true;
        invoiceId = nextInvoiceId++;
        _invoices[invoiceId] = Invoice(invoiceHash, seller, buyer, faceValue, dueDate, Status.Registered);
        emit InvoiceRegistered(invoiceId, invoiceHash, seller, buyer, faceValue, dueDate, msg.sender);
    }

    /// @notice Seller cancels an invoice that has not been settled. The hash stays used.
    function cancel(uint256 invoiceId) external {
        Invoice storage inv = _invoices[invoiceId];
        if (msg.sender != inv.seller) revert NotSeller();
        Status s = inv.status;
        if (s != Status.Registered && s != Status.Confirmed) revert InvalidStatus(invoiceId, s);
        inv.status = Status.Cancelled;
        emit InvoiceCancelled(invoiceId);
    }

    // ----------------------------------------------------------------- buyer confirmation

    function confirmByBuyer(uint256 invoiceId) external {
        Invoice storage inv = _invoices[invoiceId];
        if (msg.sender != inv.buyer) revert NotBuyer();
        _confirm(invoiceId, inv, false);
    }

    /// @notice Anyone may relay a buyer's EIP-712 InvoiceConfirmation signature.
    function confirmByBuyerWithSig(uint256 invoiceId, bytes calldata signature) external {
        Invoice storage inv = _invoices[invoiceId];
        (address signer, ECDSA.RecoverError err,) = ECDSA.tryRecover(confirmationDigest(invoiceId), signature);
        if (err != ECDSA.RecoverError.NoError || signer == address(0) || signer != inv.buyer) {
            revert InvalidBuyerSignature();
        }
        _confirm(invoiceId, inv, true);
    }

    function confirmationDigest(uint256 invoiceId) public view returns (bytes32) {
        Invoice storage inv = _invoices[invoiceId];
        return _hashTypedDataV4(
            keccak256(abi.encode(CONFIRMATION_TYPEHASH, invoiceId, inv.invoiceHash, inv.faceValue, inv.dueDate))
        );
    }

    function _confirm(uint256 invoiceId, Invoice storage inv, bool viaSignature) private {
        if (inv.status != Status.Registered) revert InvalidStatus(invoiceId, inv.status);
        inv.status = Status.Confirmed;
        emit InvoiceConfirmed(invoiceId, inv.buyer, viaSignature);
    }

    // ----------------------------------------------------------------- settlement (ledger only)

    function markSettled(uint256 invoiceId) external onlyRole(SETTLER_ROLE) {
        Invoice storage inv = _invoices[invoiceId];
        if (inv.status != Status.Confirmed) revert InvalidStatus(invoiceId, inv.status);
        inv.status = Status.Settled;
        emit InvoiceSettled(invoiceId);
    }

    // ----------------------------------------------------------------- views

    function getInvoice(uint256 invoiceId) external view returns (Invoice memory) {
        return _invoices[invoiceId];
    }

    function statusOf(uint256 invoiceId) external view returns (Status) {
        return _invoices[invoiceId].status;
    }

    function domainSeparator() external view returns (bytes32) {
        return _domainSeparatorV4();
    }
}
