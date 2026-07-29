"""Confirmation-gated, replay-safe USDT chain watcher."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.models.reseller import Payment, PaymentStatus
from app.models.usdt import (
    UsdtChainCursor,
    UsdtDeposit,
    UsdtDepositStatus,
    UsdtTransferEvent,
)
from app.services.notification_service import (
    NotificationEvent,
    NotificationService,
)
from app.services.payments.orchestrator import PaymentOrchestrator
from app.services.payments.usdt_crypto import normalize_eth_address
from app.services.payments.usdt_rpc import (
    DecodedTransfer,
    EthereumRpcClient,
    EthereumRpcError,
    decode_transfer_log,
)
from app.services.payments.webhooks import PaymentWebhookService


_ADDRESS_BATCH_SIZE = 250
_TERMINAL_SWEEP_STATUSES = {
    UsdtDepositStatus.SWEEPING,
    UsdtDepositStatus.SWEPT,
}


def iter_block_chunks(
    start_block: int, end_block: int, chunk_size: int
) -> Iterable[tuple[int, int]]:
    if start_block < 0 or end_block < start_block or chunk_size <= 0:
        raise ValueError("Invalid Ethereum block chunk range")
    current = start_block
    while current <= end_block:
        chunk_end = min(end_block, current + chunk_size - 1)
        yield current, chunk_end
        current = chunk_end + 1


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _hex_quantity(value, field: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise EthereumRpcError(f"Invalid {field} in Ethereum response")
    try:
        return int(value, 16)
    except ValueError as exc:
        raise EthereumRpcError(f"Invalid {field} in Ethereum response") from exc


class UsdtWatcher:
    def __init__(
        self,
        rpc: EthereumRpcClient,
        config: Settings = settings,
    ) -> None:
        self.rpc = rpc
        self.config = config
        if not config.usdt_enabled:
            raise EthereumRpcError("USDT watcher is not configured")
        self.chain_id = int(config.usdt_chain_id)
        self.contract = normalize_eth_address(str(config.usdt_contract_address))

    def cursor(self, db: Session, *, for_update: bool = False):
        stmt = select(UsdtChainCursor).where(
            UsdtChainCursor.chain_id == self.chain_id,
            UsdtChainCursor.contract_address == self.contract,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return db.execute(stmt).scalar_one_or_none()

    def acquire_lease(
        self,
        db: Session,
        *,
        owner: str,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        cursor = self.cursor(db)
        if cursor is None:
            return False
        result = db.execute(
            update(UsdtChainCursor)
            .where(
                UsdtChainCursor.id == cursor.id,
                or_(
                    UsdtChainCursor.lease_owner.is_(None),
                    UsdtChainCursor.lease_expires_at.is_(None),
                    UsdtChainCursor.lease_expires_at <= current,
                    UsdtChainCursor.lease_owner == owner,
                ),
            )
            .values(
                lease_owner=owner,
                lease_expires_at=current
                + timedelta(seconds=self.config.usdt_watcher_lease_seconds),
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def release_lease(self, db: Session, *, owner: str) -> None:
        db.execute(
            update(UsdtChainCursor)
            .where(
                UsdtChainCursor.chain_id == self.chain_id,
                UsdtChainCursor.contract_address == self.contract,
                UsdtChainCursor.lease_owner == owner,
            )
            .values(lease_owner=None, lease_expires_at=None)
        )

    def run_once(
        self,
        db: Session,
        *,
        lease_owner: str | None = None,
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(timezone.utc)
        self.rpc.assert_chain_id(self.chain_id)
        cursor = self.cursor(db, for_update=True)
        if cursor is None:
            return 0
        if lease_owner is not None and cursor.lease_owner != lease_owner:
            return 0

        latest = self.rpc.block_number()
        self._validate_cursor_and_rewind(db, cursor, current)
        self._recheck_recent_confirmations(db, latest, current)

        safe_block = latest - self.config.usdt_confirmations
        processed = 0
        if safe_block >= 0 and cursor.next_block <= safe_block:
            chunk_start, chunk_end = next(
                iter(
                    iter_block_chunks(
                        int(cursor.next_block),
                        int(safe_block),
                        self.config.usdt_scan_chunk_size,
                    )
                )
            )
            processed = self._scan_chunk(db, chunk_start, chunk_end, current)
            end_block = self.rpc.get_block_by_number(chunk_end)
            cursor.next_block = chunk_end + 1
            cursor.last_safe_block = chunk_end
            cursor.last_safe_block_hash = str(end_block["hash"]).lower()

        self._expire_unpaid_deposits(db, current)
        db.flush()
        return processed

    def reconcile_deposit(
        self,
        db: Session,
        deposit_id: int,
        *,
        now: datetime | None = None,
    ) -> UsdtDeposit:
        deposit = db.execute(
            select(UsdtDeposit)
            .where(UsdtDeposit.id == deposit_id)
            .with_for_update()
        ).scalar_one()
        self._reconcile(db, deposit, now or datetime.now(timezone.utc))
        db.flush()
        return deposit

    def _validate_cursor_and_rewind(
        self,
        db: Session,
        cursor: UsdtChainCursor,
        now: datetime,
    ) -> None:
        if cursor.last_safe_block is None or not cursor.last_safe_block_hash:
            return
        block = self.rpc.get_block_by_number(int(cursor.last_safe_block))
        if str(block["hash"]).lower() == cursor.last_safe_block_hash.lower():
            return
        rewind = max(
            0,
            int(cursor.last_safe_block) - self.config.usdt_reorg_recheck_blocks + 1,
        )
        cursor.next_block = min(int(cursor.next_block), rewind)
        cursor.last_safe_block = None
        cursor.last_safe_block_hash = None
        events = list(
            db.execute(
                select(UsdtTransferEvent).where(
                    UsdtTransferEvent.chain_id == self.chain_id,
                    UsdtTransferEvent.block_number >= rewind,
                    UsdtTransferEvent.reversed_at.is_(None),
                )
            ).scalars()
        )
        affected: set[int] = set()
        for event in events:
            if not self._event_is_canonical(event):
                event.reversed_at = now
                affected.add(event.deposit_id)
        for deposit_id in affected:
            deposit = db.get(UsdtDeposit, deposit_id)
            if deposit is not None:
                self._handle_reorg(db, deposit)

    def _scan_chunk(
        self,
        db: Session,
        start_block: int,
        end_block: int,
        now: datetime,
    ) -> int:
        deposits = list(
            db.execute(
                select(UsdtDeposit).where(
                    UsdtDeposit.chain_id == self.chain_id,
                    UsdtDeposit.contract_address == self.contract,
                    UsdtDeposit.status.notin_(
                        [
                            UsdtDepositStatus.REORGED,
                            UsdtDepositStatus.MANUAL_REVIEW,
                        ]
                    ),
                )
            ).scalars()
        )
        by_address = {
            normalize_eth_address(deposit.deposit_address): deposit
            for deposit in deposits
        }
        logs: list[dict] = []
        addresses = sorted(by_address)
        for offset in range(0, len(addresses), _ADDRESS_BATCH_SIZE):
            logs.extend(
                self.rpc.get_logs(
                    from_block=start_block,
                    to_block=end_block,
                    contract_address=self.contract,
                    destination_addresses=addresses[
                        offset : offset + _ADDRESS_BATCH_SIZE
                    ],
                )
            )

        unique_transfers: dict[tuple[str, int], DecodedTransfer] = {}
        for item in logs:
            transfer = decode_transfer_log(item, expected_contract=self.contract)
            key = (transfer.tx_hash, transfer.log_index)
            previous = unique_transfers.get(key)
            if previous is not None and previous != transfer:
                raise EthereumRpcError("Duplicate USDT log identity has conflicting data")
            unique_transfers[key] = transfer
        decoded = list(unique_transfers.values())
        decoded.sort(
            key=lambda item: (item.block_number, item.log_index, item.tx_hash)
        )
        affected: set[int] = set()
        block_cache: dict[int, dict] = {}
        for transfer in decoded:
            deposit = by_address.get(transfer.to_address)
            if deposit is None:
                continue
            affected.add(deposit.id)
            self._record_transfer(
                db, deposit, transfer, now=now, block_cache=block_cache
            )
        # Production and tests intentionally use autoflush=False sessions.
        # Persist event inserts before aggregation so split transfers are visible.
        db.flush()
        for deposit_id in affected:
            deposit = db.execute(
                select(UsdtDeposit)
                .where(UsdtDeposit.id == deposit_id)
                .with_for_update()
            ).scalar_one()
            self._reconcile(db, deposit, now)
        return len(decoded)

    def _record_transfer(
        self,
        db: Session,
        deposit: UsdtDeposit,
        transfer: DecodedTransfer,
        *,
        now: datetime,
        block_cache: dict[int, dict],
    ) -> None:
        existing = db.execute(
            select(UsdtTransferEvent).where(
                UsdtTransferEvent.chain_id == self.chain_id,
                UsdtTransferEvent.tx_hash == transfer.tx_hash,
                UsdtTransferEvent.log_index == transfer.log_index,
            )
        ).scalar_one_or_none()
        if transfer.removed:
            if existing is not None and existing.reversed_at is None:
                existing.reversed_at = now
            return

        receipt = self.rpc.get_transaction_receipt(transfer.tx_hash)
        if not self._receipt_matches_transfer(receipt, transfer):
            raise EthereumRpcError("USDT transfer receipt is not canonical")
        block = block_cache.setdefault(
            transfer.block_number,
            self.rpc.get_block_by_number(transfer.block_number),
        )
        if str(block["hash"]).lower() != transfer.block_hash:
            raise EthereumRpcError("USDT transfer block is not canonical")
        timestamp = datetime.fromtimestamp(
            _hex_quantity(block["timestamp"], "block timestamp"),
            tz=timezone.utc,
        )

        if existing is None:
            db.add(
                UsdtTransferEvent(
                    deposit_id=deposit.id,
                    chain_id=self.chain_id,
                    tx_hash=transfer.tx_hash,
                    log_index=transfer.log_index,
                    block_number=transfer.block_number,
                    block_hash=transfer.block_hash,
                    block_timestamp=timestamp,
                    from_address=transfer.from_address,
                    to_address=transfer.to_address,
                    token_units=transfer.token_units,
                    observed_at=now,
                    confirmed_at=now,
                )
            )
            return
        if existing.deposit_id != deposit.id:
            raise EthereumRpcError("USDT transfer destination ownership changed")
        if existing.reversed_at is None and (
            existing.block_number != transfer.block_number
            or existing.block_hash.lower() != transfer.block_hash
            or existing.token_units != transfer.token_units
        ):
            raise EthereumRpcError("USDT transfer identity changed unexpectedly")
        existing.block_number = transfer.block_number
        existing.block_hash = transfer.block_hash
        existing.block_timestamp = timestamp
        existing.from_address = transfer.from_address
        existing.to_address = transfer.to_address
        existing.token_units = transfer.token_units
        existing.confirmed_at = existing.confirmed_at or now
        existing.reversed_at = None

    @staticmethod
    def _receipt_matches_transfer(
        receipt: dict | None, transfer: DecodedTransfer
    ) -> bool:
        if receipt is None:
            return False
        return (
            _hex_quantity(receipt.get("status"), "receipt status") == 1
            and str(receipt.get("transactionHash", "")).lower()
            == transfer.tx_hash
            and _hex_quantity(receipt.get("blockNumber"), "receipt block")
            == transfer.block_number
            and str(receipt.get("blockHash", "")).lower()
            == transfer.block_hash
        )

    def _event_is_canonical(self, event: UsdtTransferEvent) -> bool:
        receipt = self.rpc.get_transaction_receipt(event.tx_hash)
        if receipt is None:
            return False
        if (
            _hex_quantity(receipt.get("status"), "receipt status") != 1
            or _hex_quantity(receipt.get("blockNumber"), "receipt block")
            != event.block_number
            or str(receipt.get("blockHash", "")).lower()
            != event.block_hash.lower()
        ):
            return False
        block = self.rpc.get_block_by_number(int(event.block_number))
        return str(block["hash"]).lower() == event.block_hash.lower()

    def _active_events(
        self, db: Session, deposit_id: int
    ) -> list[UsdtTransferEvent]:
        return list(
            db.execute(
                select(UsdtTransferEvent)
                .where(
                    UsdtTransferEvent.deposit_id == deposit_id,
                    UsdtTransferEvent.reversed_at.is_(None),
                )
                .order_by(
                    UsdtTransferEvent.block_number,
                    UsdtTransferEvent.log_index,
                )
            ).scalars()
        )

    def _reconcile(
        self,
        db: Session,
        deposit: UsdtDeposit,
        now: datetime,
    ) -> None:
        previously_empty = int(deposit.received_token_units) == 0
        events = self._active_events(db, deposit.id)
        if not self._summarize_events(deposit, events, now):
            return
        if previously_empty:
            self._notify_usdt_detection(db, deposit)
        if any(
            event.block_timestamp is None
            or _utc(event.block_timestamp) > _utc(deposit.expires_at)
            for event in events
        ):
            deposit.status = UsdtDepositStatus.MANUAL_REVIEW
            return

        expected = int(deposit.expected_token_units)
        received = int(deposit.received_token_units)
        threshold = max(1, expected - self.config.usdt_tolerance_token_units)
        if received < threshold:
            deposit.status = (
                UsdtDepositStatus.MANUAL_REVIEW
                if _utc(deposit.expires_at) <= now
                else UsdtDepositStatus.UNDERPAID
            )
            return
        self._settle_eligible_deposit(db, deposit, events, now)

    @staticmethod
    def _notify_usdt_detection(db: Session, deposit: UsdtDeposit) -> None:
        try:
            NotificationService.enqueue(
                db,
                reseller=deposit.reseller,
                event=NotificationEvent.USDT_DETECTED,
                idempotency_key=f"usdt-deposit:{deposit.id}:detected",
                data={
                    "invoice_id": deposit.invoice_id,
                    "amount_cents": deposit.invoice.amount_cents,
                    "currency": deposit.invoice.currency,
                },
            )
        except ValueError:
            return

    @staticmethod
    def _summarize_events(
        deposit: UsdtDeposit,
        events: list[UsdtTransferEvent],
        now: datetime,
    ) -> bool:
        deposit.received_token_units = sum(event.token_units for event in events)
        if not events:
            deposit.first_block = None
            deposit.confirmed_block = None
            deposit.block_hash = None
            if deposit.credited_at is None:
                deposit.status = (
                    UsdtDepositStatus.EXPIRED
                    if _utc(deposit.expires_at) <= now
                    else UsdtDepositStatus.AWAITING
                )
            return False
        deposit.first_block = min(event.block_number for event in events)
        final_event = max(
            events, key=lambda event: (event.block_number, event.log_index)
        )
        deposit.confirmed_block = final_event.block_number
        deposit.block_hash = final_event.block_hash
        return True

    def _settle_eligible_deposit(
        self,
        db: Session,
        deposit: UsdtDeposit,
        events: list[UsdtTransferEvent],
        now: datetime,
    ) -> None:
        expected = int(deposit.expected_token_units)
        received = int(deposit.received_token_units)
        payment = db.execute(
            select(Payment)
            .where(
                Payment.gateway == "usdt",
                Payment.external_ref == f"usdt-deposit:{deposit.id}",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if payment is None:
            deposit.status = UsdtDepositStatus.MANUAL_REVIEW
            return
        if payment.status == PaymentStatus.REFUNDED:
            deposit.status = UsdtDepositStatus.REORGED
            return
        if payment.status != PaymentStatus.SUCCEEDED:
            if not all(self._event_is_canonical(event) for event in events):
                deposit.status = UsdtDepositStatus.REORGED
                return
            PaymentOrchestrator.finalize_success(db, payment)
            # A second canonical check closes the RPC/credit race window. An
            # exception rolls the enclosing DB transaction back.
            if not all(self._event_is_canonical(event) for event in events):
                raise EthereumRpcError(
                    "USDT canonical chain changed during finalization"
                )
            deposit.credited_at = now

        if received > expected:
            deposit.status = UsdtDepositStatus.OVERPAID
        elif deposit.status not in _TERMINAL_SWEEP_STATUSES:
            deposit.status = UsdtDepositStatus.SWEEP_PENDING

    def _recheck_recent_confirmations(
        self,
        db: Session,
        latest_block: int,
        now: datetime,
    ) -> None:
        floor = max(0, latest_block - self.config.usdt_reorg_recheck_blocks)
        events = list(
            db.execute(
                select(UsdtTransferEvent)
                .join(UsdtDeposit)
                .where(
                    UsdtTransferEvent.chain_id == self.chain_id,
                    UsdtTransferEvent.block_number >= floor,
                    UsdtTransferEvent.confirmed_at.is_not(None),
                    UsdtTransferEvent.reversed_at.is_(None),
                    UsdtDeposit.credited_at.is_not(None),
                )
            ).scalars()
        )
        for event in events:
            if self._event_is_canonical(event):
                continue
            event.reversed_at = now
            deposit = db.get(UsdtDeposit, event.deposit_id)
            if deposit is not None:
                self._handle_reorg(db, deposit)

    def _handle_reorg(
        self,
        db: Session,
        deposit: UsdtDeposit,
    ) -> None:
        deposit.status = UsdtDepositStatus.REORGED
        db.flush()
        deposit.received_token_units = int(
            db.scalar(
                select(func.coalesce(func.sum(UsdtTransferEvent.token_units), 0))
                .where(
                    UsdtTransferEvent.deposit_id == deposit.id,
                    UsdtTransferEvent.reversed_at.is_(None),
                )
            )
            or 0
        )
        payment = db.execute(
            select(Payment)
            .where(
                Payment.gateway == "usdt",
                Payment.external_ref == f"usdt-deposit:{deposit.id}",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if payment is not None and payment.status == PaymentStatus.SUCCEEDED:
            PaymentWebhookService.reverse_payment(
                db,
                gateway="usdt",
                external_ref=payment.external_ref or "",
                event_id=f"reorg:{deposit.id}",
                reason="reorg",
            )
        db.flush()

    def _expire_unpaid_deposits(self, db: Session, now: datetime) -> None:
        deposits = list(
            db.execute(
                select(UsdtDeposit).where(
                    UsdtDeposit.chain_id == self.chain_id,
                    UsdtDeposit.contract_address == self.contract,
                    UsdtDeposit.credited_at.is_(None),
                    UsdtDeposit.expires_at <= now,
                    UsdtDeposit.status.in_(
                        [
                            UsdtDepositStatus.AWAITING,
                            UsdtDepositStatus.PENDING_CONFIRMATIONS,
                            UsdtDepositStatus.UNDERPAID,
                        ]
                    ),
                )
            ).scalars()
        )
        for deposit in deposits:
            deposit.status = (
                UsdtDepositStatus.MANUAL_REVIEW
                if int(deposit.received_token_units) > 0
                else UsdtDepositStatus.EXPIRED
            )
