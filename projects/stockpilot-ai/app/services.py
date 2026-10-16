from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import InventoryMovement, Product, SalesRecord, SalesTrackingDay
from app.schemas import AdjustmentInput, ProductInput, SaleInput


class DuplicateSKUError(ValueError):
    pass


class InvalidAdjustmentError(ValueError):
    pass


class ProductNotFoundError(ValueError):
    pass


class InsufficientStockError(ValueError):
    pass


class DuplicateSaleError(ValueError):
    pass


class DemandRangeError(ValueError):
    pass


MAX_DAILY_DEMAND_RANGE_DAYS = 3660


@dataclass(frozen=True)
class DailyDemandPoint:
    sale_date: date
    recorded_quantity: int
    is_tracked: bool

    @property
    def demand_quantity(self) -> int | None:
        """Only complete tracking days have an observed daily demand value."""
        return self.recorded_quantity if self.is_tracked else None


def create_product(db: Session, data: ProductInput) -> Product:
    product = Product(**data.model_dump())
    db.add(product)
    try:
        db.flush()
        if product.stock_quantity:
            db.add(InventoryMovement(product_id=product.id, quantity=product.stock_quantity, reason="Opening stock"))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "sku" in str(exc).lower() or "unique" in str(exc).lower():
            raise DuplicateSKUError("A product with this SKU already exists.") from exc
        raise
    db.refresh(product)
    return product


def update_product(db: Session, product: Product, data: ProductInput) -> Product:
    for key, value in data.model_dump(exclude={"stock_quantity"}).items():
        setattr(product, key, value)
    # Existing quantities can only change through a reasoned stock adjustment.
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "sku" in str(exc).lower() or "unique" in str(exc).lower():
            raise DuplicateSKUError("A product with this SKU already exists.") from exc
        raise
    db.refresh(product)
    return product


def adjust_stock(db: Session, product_id: int, data: AdjustmentInput) -> Product:
    delta = data.quantity if data.direction == "increase" else -data.quantity
    try:
        # The conditional update keeps SQLite adjustments atomic, including concurrent decreases.
        result = db.execute(
            update(Product)
            .where(Product.id == product_id, Product.stock_quantity + delta >= 0)
            .values(stock_quantity=Product.stock_quantity + delta)
        )
        if result.rowcount == 0:
            exists = db.scalar(select(Product.id).where(Product.id == product_id))
            db.rollback()
            if exists is None:
                raise LookupError("Product not found.")
            raise InvalidAdjustmentError("This adjustment would make stock negative.")
        db.add(InventoryMovement(product_id=product_id, quantity=delta, reason=data.reason))
        db.commit()
    except Exception:
        db.rollback()
        raise
    product = db.get(Product, product_id)
    db.refresh(product)
    return product


def dashboard_stats(db: Session) -> dict:
    count, units, value = db.execute(
        select(
            func.count(Product.id),
            func.coalesce(func.sum(Product.stock_quantity), 0),
            func.coalesce(func.sum(Product.unit_price * Product.stock_quantity), 0),
        )
    ).one()
    low_count = db.scalar(select(func.count(Product.id)).where(Product.stock_quantity <= Product.minimum_stock))
    return {"product_count": count, "total_units": units, "inventory_value": Decimal(value), "low_stock_count": low_count}


def _mark_tracking_complete(db: Session, product_id: int, tracked_date: date) -> None:
    statement = sqlite_insert(SalesTrackingDay).values(product_id=product_id, sales_date=tracked_date)
    db.execute(statement.on_conflict_do_nothing(index_elements=["product_id", "sales_date"]))


def record_sale(db: Session, data: SaleInput) -> SalesRecord:
    """Commit sale, stock decrease, movement, and optional coverage assertion atomically."""
    try:
        key = str(data.idempotency_key)
        if db.scalar(select(SalesRecord.id).where(SalesRecord.idempotency_key == key)) is not None:
            raise DuplicateSaleError("This sale form was already submitted. Refresh the page to record another sale.")
        product = db.get(Product, data.product_id)
        if product is None:
            raise ProductNotFoundError("Select an existing product.")
        price = data.unit_selling_price if data.unit_selling_price is not None else product.unit_price

        result = db.execute(
            update(Product)
            .where(Product.id == data.product_id, Product.stock_quantity >= data.quantity_sold)
            .values(stock_quantity=Product.stock_quantity - data.quantity_sold)
        )
        if result.rowcount != 1:
            db.rollback()
            if db.get(Product, data.product_id) is None:
                raise ProductNotFoundError("Select an existing product.")
            raise InsufficientStockError("There is not enough stock to record this sale.")

        sale = SalesRecord(
            product_id=data.product_id,
            sale_date=data.sale_date,
            quantity_sold=data.quantity_sold,
            unit_selling_price=price,
            idempotency_key=key,
        )
        db.add(sale)
        db.flush()
        db.add(InventoryMovement(
            product_id=data.product_id,
            quantity=-data.quantity_sold,
            reason=f"Sale recorded (sales record #{sale.id})",
            sales_record_id=sale.id,
        ))
        if data.mark_day_complete:
            _mark_tracking_complete(db, data.product_id, data.sale_date)
        db.commit()
        db.refresh(sale)
        return sale
    except IntegrityError as exc:
        db.rollback()
        if db.scalar(select(SalesRecord.id).where(SalesRecord.idempotency_key == str(data.idempotency_key))) is not None:
            raise DuplicateSaleError("This sale form was already submitted. Refresh the page to record another sale.") from exc
        raise
    except Exception:
        db.rollback()
        raise


def mark_sales_day_complete(db: Session, product_id: int, tracked_date: date) -> None:
    """Explicitly assert a product/date is fully tracked, including a zero-sale day."""
    try:
        if db.get(Product, product_id) is None:
            raise ProductNotFoundError("Select an existing product.")
        _mark_tracking_complete(db, product_id, tracked_date)
        db.commit()
    except Exception:
        db.rollback()
        raise


def daily_demand_summary(
    db: Session, product_id: int, start_date: date, end_date: date
) -> list[DailyDemandPoint]:
    """Return every date in range; untracked days have no demand value, not zero."""
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")
    if (end_date - start_date).days + 1 > MAX_DAILY_DEMAND_RANGE_DAYS:
        raise DemandRangeError(f"Choose a date range of {MAX_DAILY_DEMAND_RANGE_DAYS} days or fewer.")
    totals = dict(db.execute(
        select(SalesRecord.sale_date, func.sum(SalesRecord.quantity_sold))
        .where(
            SalesRecord.product_id == product_id,
            SalesRecord.sale_date >= start_date,
            SalesRecord.sale_date <= end_date,
        )
        .group_by(SalesRecord.sale_date)
    ).all())
    tracked_dates = set(db.scalars(
        select(SalesTrackingDay.sales_date).where(
            SalesTrackingDay.product_id == product_id,
            SalesTrackingDay.sales_date >= start_date,
            SalesTrackingDay.sales_date <= end_date,
        )
    ).all())
    points = []
    day = start_date
    while day <= end_date:
        points.append(DailyDemandPoint(
            sale_date=day,
            recorded_quantity=int(totals.get(day, 0)),
            is_tracked=day in tracked_dates,
        ))
        day += timedelta(days=1)
    return points


@dataclass(frozen=True)
class DemandForecast:
    """A transparent baseline estimate from fully tracked daily demand."""

    daily_average: Decimal
    weekly_estimate: Decimal
    tracked_days: int
    lookback_days: int


def simple_demand_forecast(
    db: Session, product_id: int, as_of: date | None = None
) -> DemandForecast | None:
    """Estimate the next week's demand from tracked days in the previous 28 days.

    Requires at least seven fully tracked days. Incomplete dates are excluded,
    rather than being treated as days with zero demand.
    """
    today = as_of or date.today()
    lookback_days = 28
    history = daily_demand_summary(
        db, product_id, today - timedelta(days=lookback_days), today - timedelta(days=1)
    )
    observed = [point.demand_quantity for point in history if point.demand_quantity is not None]
    if len(observed) < 7:
        return None

    daily_average = Decimal(sum(observed)) / Decimal(len(observed))
    return DemandForecast(
        daily_average=daily_average,
        weekly_estimate=daily_average * Decimal(7),
        tracked_days=len(observed),
        lookback_days=lookback_days,
    )
