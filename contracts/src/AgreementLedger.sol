// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {InvoiceRegistry} from "./InvoiceRegistry.sol";
import {ChainTime} from "./ChainTime.sol";

/// @title AgreementLedger
/// @notice Shared record of early-payment agreements so supplier, buyer and auditors can verify
///         the same facts: which exact terms were committed, who approved them, when the bank
///         payment was submitted and when it settled.
/// @dev The agreement hash is the EIP-712 digest of the `Agreement` struct under this contract's
///      domain, computed on-chain in `commitAgreement`. Each company's authorized human signs that
///      digest (tier T3). Any field change gives a new version and a new hash, so old signatures are
///      useless. Only the deterministic coordinator holds COORDINATOR_ROLE; AI agents have no key.
contract AgreementLedger is AccessControl, Pausable, EIP712 {
    bytes32 public constant COORDINATOR_ROLE = keccak256("COORDINATOR_ROLE");

    /// @dev Off-chain strings are passed as keccak256 hashes: agreementId, payeeAccount, idempotencyKey.
    ///      `currency` is an ISO 4217 code packed into bytes32 (e.g. "TRY").
    bytes32 public constant AGREEMENT_TYPEHASH = keccak256(
        "Agreement(bytes32 agreementId,uint32 version,uint256 invoiceId,bytes32 invoiceHash,address seller,address buyer,uint256 faceAmount,uint32 discountBps,uint256 payAmount,bytes32 currency,bytes32 payeeAccountHash,uint64 expiresAt,bytes32 idempotencyKeyHash)"
    );

    uint256 public constant BPS = 10_000;

    enum Status {
        None,
        Committed,
        Submitted,
        Settled,
        Failed,
        Superseded
    }

    struct Agreement {
        bytes32 agreementId;
        uint32 version;
        uint256 invoiceId;
        bytes32 invoiceHash;
        address seller;
        address buyer;
        uint256 faceAmount;
        uint32 discountBps;
        uint256 payAmount;
        bytes32 currency;
        bytes32 payeeAccountHash;
        uint64 expiresAt;
        bytes32 idempotencyKeyHash;
    }

    struct Record {
        uint256 invoiceId;
        uint32 version;
        uint64 expiresAt;
        address seller;
        address buyer;
        Status status;
        bool sellerApproved;
        bool buyerApproved;
        bytes32 bankPaymentIdHash;
        bytes32 settlementReceiptHash;
    }

    InvoiceRegistry public immutable registry;

    mapping(bytes32 => Record) private _records;
    mapping(uint256 => bytes32) public activeAgreementOf; // invoiceId => agreement hash (0 if none)
    mapping(uint256 => uint32) public latestVersionOf; // invoiceId => highest committed version

    error InvoiceNotConfirmed(uint256 invoiceId);
    error AgreementMismatch();
    error InvalidTerms();
    error ActiveAgreementExists(bytes32 activeHash);
    error VersionNotIncreasing(uint32 version, uint32 latest);
    error AlreadyCommitted(bytes32 agreementHash);
    error InvalidStatus(bytes32 agreementHash, Status status);
    error AgreementExpired(bytes32 agreementHash);
    error InvalidSigner(address signer);
    error AlreadyApproved(bytes32 agreementHash, address signer);
    error MissingApprovals(bool sellerApproved, bool buyerApproved);
    error ZeroReference();

    event AgreementCommitted(
        bytes32 indexed agreementHash, uint256 indexed invoiceId, uint32 version, uint256 payAmount, uint64 expiresAt
    );
    event AgreementApproved(bytes32 indexed agreementHash, address indexed signer, bool isSeller);
    event AgreementSuperseded(bytes32 indexed agreementHash, uint256 indexed invoiceId);
    event PaymentSubmitted(bytes32 indexed agreementHash, bytes32 bankPaymentIdHash);
    event PaymentSettled(bytes32 indexed agreementHash, uint256 indexed invoiceId, bytes32 settlementReceiptHash);
    event PaymentFailed(bytes32 indexed agreementHash, bytes32 reasonHash);

    constructor(address admin, InvoiceRegistry registry_) EIP712("AuditFlow AgreementLedger", "1") {
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        registry = registry_;
    }

    // ----------------------------------------------------------------- hashing

    function hashAgreement(Agreement calldata a) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(AGREEMENT_TYPEHASH, a)));
    }

    // ----------------------------------------------------------------- coordinator (T4)

    /// @notice Commits the exact terms. The invoice must be buyer-confirmed and the parties,
    ///         invoice hash and amounts must match the registry; payAmount is checked on-chain.
    function commitAgreement(Agreement calldata a)
        external
        onlyRole(COORDINATOR_ROLE)
        whenNotPaused
        returns (bytes32 agreementHash)
    {
        InvoiceRegistry.Invoice memory inv = registry.getInvoice(a.invoiceId);
        if (inv.status != InvoiceRegistry.Status.Confirmed) revert InvoiceNotConfirmed(a.invoiceId);
        if (a.invoiceHash != inv.invoiceHash || a.seller != inv.seller || a.buyer != inv.buyer) {
            revert AgreementMismatch();
        }
        if (
            a.faceAmount == 0 || a.faceAmount > inv.faceValue || a.discountBps >= BPS
                || a.payAmount != (a.faceAmount * (BPS - a.discountBps)) / BPS || a.expiresAt <= ChainTime.nowSeconds()
        ) revert InvalidTerms();

        bytes32 active = activeAgreementOf[a.invoiceId];
        if (active != bytes32(0)) revert ActiveAgreementExists(active);
        uint32 latest = latestVersionOf[a.invoiceId];
        if (a.version <= latest) revert VersionNotIncreasing(a.version, latest);

        agreementHash = hashAgreement(a);
        if (_records[agreementHash].status != Status.None) revert AlreadyCommitted(agreementHash);

        _records[agreementHash] = Record({
            invoiceId: a.invoiceId,
            version: a.version,
            expiresAt: a.expiresAt,
            seller: a.seller,
            buyer: a.buyer,
            status: Status.Committed,
            sellerApproved: false,
            buyerApproved: false,
            bankPaymentIdHash: bytes32(0),
            settlementReceiptHash: bytes32(0)
        });
        activeAgreementOf[a.invoiceId] = agreementHash;
        latestVersionOf[a.invoiceId] = a.version;
        emit AgreementCommitted(agreementHash, a.invoiceId, a.version, a.payAmount, a.expiresAt);
    }

    /// @notice Retires a committed (not yet submitted) version, e.g. before committing changed terms.
    ///         Its approvals can no longer be used.
    function supersede(bytes32 agreementHash) external onlyRole(COORDINATOR_ROLE) {
        Record storage r = _records[agreementHash];
        if (r.status != Status.Committed) revert InvalidStatus(agreementHash, r.status);
        r.status = Status.Superseded;
        delete activeAgreementOf[r.invoiceId];
        emit AgreementSuperseded(agreementHash, r.invoiceId);
    }

    /// @notice Records that the bank payment was initiated. Requires both human approvals.
    function recordSubmission(bytes32 agreementHash, bytes32 bankPaymentIdHash)
        external
        onlyRole(COORDINATOR_ROLE)
        whenNotPaused
    {
        Record storage r = _records[agreementHash];
        if (r.status != Status.Committed) revert InvalidStatus(agreementHash, r.status);
        if (!r.sellerApproved || !r.buyerApproved) revert MissingApprovals(r.sellerApproved, r.buyerApproved);
        if (ChainTime.nowSeconds() > r.expiresAt) revert AgreementExpired(agreementHash);
        if (registry.statusOf(r.invoiceId) != InvoiceRegistry.Status.Confirmed) {
            revert InvoiceNotConfirmed(r.invoiceId);
        }
        if (bankPaymentIdHash == bytes32(0)) revert ZeroReference();
        r.status = Status.Submitted;
        r.bankPaymentIdHash = bankPaymentIdHash;
        emit PaymentSubmitted(agreementHash, bankPaymentIdHash);
    }

    /// @notice Records a verified bank settlement and marks the invoice Settled.
    /// @dev Not pausable: once money moved at the bank, the ledger must be able to reflect it.
    function recordSettlement(bytes32 agreementHash, bytes32 settlementReceiptHash)
        external
        onlyRole(COORDINATOR_ROLE)
    {
        Record storage r = _records[agreementHash];
        if (r.status != Status.Submitted) revert InvalidStatus(agreementHash, r.status);
        if (settlementReceiptHash == bytes32(0)) revert ZeroReference();
        r.status = Status.Settled;
        r.settlementReceiptHash = settlementReceiptHash;
        delete activeAgreementOf[r.invoiceId];
        emit PaymentSettled(agreementHash, r.invoiceId, settlementReceiptHash);
        registry.markSettled(r.invoiceId);
    }

    /// @notice Records a failed bank payment; the invoice becomes free for a new agreement version.
    /// @dev Not pausable, for the same reason as recordSettlement.
    function recordFailure(bytes32 agreementHash, bytes32 reasonHash) external onlyRole(COORDINATOR_ROLE) {
        Record storage r = _records[agreementHash];
        if (r.status != Status.Submitted) revert InvalidStatus(agreementHash, r.status);
        r.status = Status.Failed;
        delete activeAgreementOf[r.invoiceId];
        emit PaymentFailed(agreementHash, reasonHash);
    }

    // ----------------------------------------------------------------- humans (T3)

    /// @notice Anyone may relay a signature; it must come from the invoice's seller or buyer.
    function approve(bytes32 agreementHash, bytes calldata signature) external whenNotPaused {
        Record storage r = _records[agreementHash];
        if (r.status != Status.Committed) revert InvalidStatus(agreementHash, r.status);
        if (ChainTime.nowSeconds() > r.expiresAt) revert AgreementExpired(agreementHash);

        (address signer, ECDSA.RecoverError err,) = ECDSA.tryRecover(agreementHash, signature);
        if (err != ECDSA.RecoverError.NoError) revert InvalidSigner(address(0));

        bool isSeller = false;
        if (signer == r.seller) {
            if (r.sellerApproved) revert AlreadyApproved(agreementHash, signer);
            r.sellerApproved = true;
            isSeller = true;
        } else if (signer == r.buyer) {
            if (r.buyerApproved) revert AlreadyApproved(agreementHash, signer);
            r.buyerApproved = true;
        } else {
            revert InvalidSigner(signer);
        }
        emit AgreementApproved(agreementHash, signer, isSeller);
    }

    // ----------------------------------------------------------------- admin

    function pause() external onlyRole(DEFAULT_ADMIN_ROLE) {
        _pause();
    }

    function unpause() external onlyRole(DEFAULT_ADMIN_ROLE) {
        _unpause();
    }

    // ----------------------------------------------------------------- views

    function getRecord(bytes32 agreementHash) external view returns (Record memory) {
        return _records[agreementHash];
    }

    function isFullyApproved(bytes32 agreementHash) external view returns (bool) {
        Record storage r = _records[agreementHash];
        return r.sellerApproved && r.buyerApproved;
    }

    function domainSeparator() external view returns (bytes32) {
        return _domainSeparatorV4();
    }
}
