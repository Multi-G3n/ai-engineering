"""Read-only CSV previews and atomic historical ingestion; never adjust inventory."""

import base64
import binascii
import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import hashlib
import io
import secrets
from uuid import UUID

from itsdangerous import BadData, URLSafeTimedSerializer
from pydantic import ValidationError
from sqlalchemy import select, text, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import session_secret_key
from app.models import HistoricalSale, HistoricalSalesCoverage, Product, SalesImportBatch, SalesRecord, SalesTrackingDay, User
from app.schemas import HistoricalSaleInput

CSV_COLUMNS = ("external_sale_id", "sale_date", "sku", "quantity", "unit_price")
MAX_FILE_BYTES = 1024 * 1024
MAX_ROWS = 5000
MAX_BODY_BYTES = 3 * 1024 * 1024
PREVIEW_TTL_SECONDS = 900
MAX_TOKEN_BYTES = 2 * 1024 * 1024


class SalesImportError(ValueError):
    """A safe, actionable import error (never an underlying database exception)."""


class ImportBodyLimitMiddleware:
    """Bound actual upload/confirmation bodies before multipart parsing or spooling."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not scope["path"].startswith("/sales/import"):
            return await self.app(scope, receive, send)
        from starlette.responses import PlainTextResponse
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                return await PlainTextResponse("Import request exceeds the 3 MiB limit.", status_code=413)(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


def safe_filename(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:160]
    return name or "upload.csv"


def validate_label(value: str, name: str, maximum: int) -> str:
    if not value or len(value) > maximum or value != value.strip() or any(ord(c) < 32 for c in value):
        raise SalesImportError(f"{name} is required, without surrounding whitespace or control characters (maximum {maximum} characters).")
    return value


@dataclass
class PreviewRow:
    line: int
    values: dict
    data: HistoricalSaleInput | None = None
    product_id: int | None = None
    status: str = "valid"
    errors: list[str] = field(default_factory=list)

    def reject(self, status: str, message: str):
        self.status = status
        self.errors.append(message)


@dataclass
class ImportPreview:
    filename: str
    source: str
    rows: list[PreviewRow]
    file_errors: list[str] = field(default_factory=list)

    @property
    def can_import(self):
        return bool(self.rows) and not self.file_errors and not any(r.status in ("invalid", "conflict") for r in self.rows)

    def count(self, status):
        return sum(r.status == status for r in self.rows)

    @property
    def date_range(self):
        dates = [r.data.sale_date for r in self.rows if r.data and r.status in ("valid", "duplicate")]
        return (min(dates), max(dates)) if dates else None

    @property
    def product_count(self):
        return len({r.product_id for r in self.rows if r.product_id and r.status in ("valid", "duplicate")})

    @property
    def total_quantity(self):
        return sum(r.data.quantity for r in self.rows if r.status == "valid")

    @property
    def sales_value(self):
        return sum((r.data.unit_price * r.data.quantity for r in self.rows if r.status == "valid"), Decimal("0.00"))


def _identity(row: PreviewRow):
    return (row.product_id, row.data.sale_date, row.data.quantity, row.data.unit_price)


def preview_csv(db: Session, content: bytes, filename: str, source: str) -> ImportPreview:
    """No writes, no auto-products and no implied coverage. All problems are visible."""
    filename = safe_filename(filename)
    validate_label(source, "Source", 100)
    result = ImportPreview(filename, source, [])
    if not content or len(content) > MAX_FILE_BYTES:
        result.file_errors.append("CSV must be nonempty and at most 1 MiB.")
        return result
    try:
        decoded = content.decode("utf-8-sig")
        if "\x00" in decoded:
            raise ValueError
    except (UnicodeError, ValueError):
        result.file_errors.append("Use UTF-8 CSV without NUL characters; UTF-8 BOM is supported.")
        return result
    reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
    try:
        header = next(reader, [])
        if len(header) != len(CSV_COLUMNS) or set(header) != set(CSV_COLUMNS):
            result.file_errors.append("Required columns (no duplicates or extra columns): " + ", ".join(CSV_COLUMNS))
            return result
        for values in reader:
            if len(result.rows) >= MAX_ROWS:
                result.file_errors.append(f"CSV exceeds the {MAX_ROWS} row limit.")
                for parsed_row in result.rows:
                    parsed_row.reject("invalid", "File row limit exceeded; full validation was not performed.")
                return result
            row = PreviewRow(reader.line_num, dict(zip(header, values)))
            result.rows.append(row)
            if len(values) != len(header) or any(len(value) > 160 for value in values):
                row.reject("invalid", "Expected exactly five fields, each at most 160 characters.")
                continue
            try:
                row.data = HistoricalSaleInput(**row.values)
            except ValidationError as exc:
                row.reject("invalid", "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors(include_input=False)))
    except csv.Error:
        result.file_errors.append("Malformed CSV; check quoting and field sizes. No rows can be imported.")
    if not result.rows:
        result.file_errors.append("CSV contains no sales rows.")
        return result
    parsed = [r for r in result.rows if r.data is not None]
    if not parsed:
        return result
    products = {p.sku: p.id for p in db.scalars(select(Product).where(Product.sku.in_({r.data.sku for r in parsed})))}
    for row in parsed:
        row.product_id = products.get(row.data.sku)
        if row.product_id is None:
            row.reject("invalid", "Unknown product SKU; create and verify the product separately.")
    eligible = [r for r in parsed if r.status == "valid"]
    if not eligible:
        return result
    pairs = {(r.product_id, r.data.sale_date) for r in eligible}
    existing = db.execute(select(HistoricalSale, SalesImportBatch.source).join(
        SalesImportBatch, HistoricalSale.import_batch_id == SalesImportBatch.id
    ).where(HistoricalSale.external_sale_id.in_({r.data.external_sale_id for r in eligible}))).all()
    by_external = {h.external_sale_id: (h, src) for h, src in existing}
    sources = {(pid, day): src for pid, day, src in db.execute(select(
        HistoricalSale.product_id, HistoricalSale.sale_date, SalesImportBatch.source
    ).join(SalesImportBatch, HistoricalSale.import_batch_id == SalesImportBatch.id).where(
        tuple_(HistoricalSale.product_id, HistoricalSale.sale_date).in_(pairs)
    ).distinct()).all()}
    operational = set(db.execute(select(SalesRecord.product_id, SalesRecord.sale_date).where(
        tuple_(SalesRecord.product_id, SalesRecord.sale_date).in_(pairs)).distinct()).all())
    operational.update(db.execute(select(SalesTrackingDay.product_id, SalesTrackingDay.sales_date).where(
        tuple_(SalesTrackingDay.product_id, SalesTrackingDay.sales_date).in_(pairs))).all())
    complete = set(db.execute(select(HistoricalSalesCoverage.product_id, HistoricalSalesCoverage.sales_date).where(
        tuple_(HistoricalSalesCoverage.product_id, HistoricalSalesCoverage.sales_date).in_(pairs))).all())
    groups = {}
    for row in eligible:
        groups.setdefault(row.data.external_sale_id, []).append(row)
    for external, group in groups.items():
        if len({_identity(r) for r in group}) > 1:
            for row in group:
                row.reject("conflict", "This external identifier has conflicting rows within the file.")
            continue
        for index, row in enumerate(group):
            previous = by_external.get(external)
            day = (row.product_id, row.data.sale_date)
            if previous:
                old, old_source = previous
                if (old.product_id, old.sale_date, old.quantity_sold, old.unit_selling_price) != _identity(row) or old_source != source:
                    row.reject("conflict", "External identifier already exists with different data or source.")
                else:
                    row.status = "duplicate"
            elif day in operational:
                row.reject("conflict", "Operational sales or complete operational coverage already exist for this product/date.")
            elif day in complete:
                row.reject("conflict", "Historical coverage is already complete; additional records require a reviewed correction.")
            elif day in sources and sources[day] != source:
                row.reject("conflict", "Another source owns historical records for this product/date.")
            elif index:
                row.status = "duplicate"
    return result


def _serializer():
    return URLSafeTimedSerializer(session_secret_key(), salt="stockpilot-historical-preview-v1")


def sign_preview(content: bytes, filename: str, source: str, user_id: int, nonce: str) -> str:
    return _serializer().dumps({"content": base64.b64encode(content).decode("ascii"),
                                "filename": safe_filename(filename), "source": source,
                                "user_id": user_id, "nonce": nonce})


def read_preview(token: str, user_id: int, nonce: str | None) -> dict:
    try:
        if len(token) > MAX_TOKEN_BYTES or not nonce:
            raise ValueError
        data = _serializer().loads(token, max_age=PREVIEW_TTL_SECONDS)
        if data["user_id"] != user_id or not secrets.compare_digest(data["nonce"], nonce):
            raise ValueError
        content = base64.b64decode(data["content"], validate=True)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError
        return {"content": content, "filename": data["filename"], "source": data["source"], "confirmation_key": str(UUID(nonce))}
    except (BadData, ValueError, KeyError, TypeError, binascii.Error):
        raise SalesImportError("Preview is invalid or expired. Upload the CSV again.") from None


def _write_lock(db):
    # A zero-row UPDATE reserves SQLite's writer lock before validation queries.
    # No batch or business record is changed; concurrent confirms serialize here.
    db.execute(text("UPDATE sales_import_batches SET id=id WHERE 0"))


def execute_import(db: Session, content: bytes, filename: str, source: str,
                   user_id: int, confirmation_key: str) -> SalesImportBatch:
    try:
        confirmation_key = str(UUID(confirmation_key))
        _write_lock(db)
        user = db.get(User, user_id, populate_existing=True)
        if user is None or not user.is_active or not user.is_admin:
            raise SalesImportError("An active administrator is required.")
        previous = db.scalar(select(SalesImportBatch).where(SalesImportBatch.confirmation_key == confirmation_key))
        digest = hashlib.sha256(content).hexdigest()
        if previous is not None:
            if previous.user_id != user_id or previous.source != source or previous.file_sha256 != digest:
                raise SalesImportError("Confirmation does not match the original import.")
            db.commit()
            return previous
        preview = preview_csv(db, content, filename, source)
        if not preview.can_import:
            raise SalesImportError("Import rejected during revalidation. Preview the CSV again and resolve all invalid or conflicting rows.")
        batch = SalesImportBatch(confirmation_key=confirmation_key, filename=preview.filename,
                                 source=source, file_sha256=digest, user_id=user_id, total_rows=len(preview.rows),
                                 imported_rows=preview.count("valid"), duplicate_rows=preview.count("duplicate"))
        db.add(batch)
        db.flush()
        for row in preview.rows:
            if row.status == "valid":
                db.add(HistoricalSale(import_batch_id=batch.id, external_sale_id=row.data.external_sale_id,
                                      product_id=row.product_id, sale_date=row.data.sale_date,
                                      quantity_sold=row.data.quantity, unit_selling_price=row.data.unit_price,
                                      source_line=row.line))
        db.commit()
        return batch
    except SalesImportError:
        db.rollback()
        raise
    except (SQLAlchemyError, ValueError):
        db.rollback()
        raise SalesImportError("Import could not be saved. No partial import was committed. Preview again before retrying.") from None
    except Exception:
        db.rollback()
        raise SalesImportError("Import failed. No partial import was committed.") from None


def declare_historical_coverage(db: Session, product_id: int, sales_date: date, source: str, reason: str, user_id: int):
    try:
        validate_label(source, "Source", 100)
        validate_label(reason, "Coverage verification reason", 240)
        if sales_date >= date.today():
            raise SalesImportError("Historical coverage must be before today.")
        _write_lock(db)
        user = db.get(User, user_id, populate_existing=True)
        if user is None or not user.is_active or not user.is_admin or db.get(Product, product_id) is None:
            raise SalesImportError("Select an existing product as an active administrator.")
        if db.scalar(select(SalesRecord.id).where(SalesRecord.product_id == product_id, SalesRecord.sale_date == sales_date)) or db.scalar(
            select(SalesTrackingDay.id).where(SalesTrackingDay.product_id == product_id, SalesTrackingDay.sales_date == sales_date)
        ):
            raise SalesImportError("Operational data or coverage already exist for this product/date.")
        sources = set(db.scalars(select(SalesImportBatch.source).join(HistoricalSale).where(
            HistoricalSale.product_id == product_id, HistoricalSale.sale_date == sales_date)))
        if sources and sources != {source}:
            raise SalesImportError("Coverage source must match the imported records for this product/date.")
        previous = db.scalar(select(HistoricalSalesCoverage).where(
            HistoricalSalesCoverage.product_id == product_id, HistoricalSalesCoverage.sales_date == sales_date))
        if previous:
            if previous.source != source:
                raise SalesImportError("This date is already complete for another source.")
            db.commit()
            return False
        db.add(HistoricalSalesCoverage(product_id=product_id, sales_date=sales_date,
                                       source=source, user_id=user_id, reason=reason))
        db.commit()
        return True
    except SalesImportError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise SalesImportError("Coverage could not be saved. No changes were committed.") from None
