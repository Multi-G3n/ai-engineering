from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("unit_price >= 0", name="ck_product_price_nonnegative"),
        CheckConstraint("stock_quantity >= 0", name="ck_product_stock_nonnegative"),
        CheckConstraint("minimum_stock >= 0", name="ck_product_minimum_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    category: Mapped[str] = mapped_column(String(100), default="Uncategorized")
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0)
    minimum_stock: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    movements: Mapped[list["InventoryMovement"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="InventoryMovement.created_at.desc()"
    )
    sales_records: Mapped[list["SalesRecord"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="SalesRecord.sale_date.desc()"
    )
    sales_tracking_days: Mapped[list["SalesTrackingDay"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    @property
    def is_low_stock(self) -> bool:
        return self.stock_quantity <= self.minimum_stock


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    __table_args__ = (CheckConstraint("quantity != 0", name="ck_movement_nonzero"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)  # positive = received, negative = removed
    reason: Mapped[str] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    sales_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_records.id", ondelete="RESTRICT"), unique=True, nullable=True
    )
    product: Mapped[Product] = relationship(back_populates="movements")
    sales_record: Mapped["SalesRecord | None"] = relationship(back_populates="stock_movement")


class SalesRecord(Base):
    """One recorded sale; daily demand is derived by summing these records."""

    __tablename__ = "sales_records"
    __table_args__ = (
        CheckConstraint("quantity_sold > 0", name="ck_sales_quantity_positive"),
        CheckConstraint("unit_selling_price >= 0", name="ck_sales_price_nonnegative"),
        UniqueConstraint("idempotency_key", name="uq_sales_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    sale_date: Mapped[date] = mapped_column(Date, index=True)
    quantity_sold: Mapped[int] = mapped_column(Integer)
    unit_selling_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Nullable only to preserve historical sales for which no form token exists.
    idempotency_key: Mapped[str | None] = mapped_column(String(36), nullable=True)
    product: Mapped[Product] = relationship(back_populates="sales_records")
    stock_movement: Mapped["InventoryMovement | None"] = relationship(back_populates="sales_record", uselist=False)


class SalesTrackingDay(Base):
    """An explicit assertion that sales for a product/date are fully recorded."""

    __tablename__ = "sales_tracking_days"
    __table_args__ = (UniqueConstraint("product_id", "sales_date", name="uq_tracking_product_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    sales_date: Mapped[date] = mapped_column(Date, index=True)
    marked_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    product: Mapped[Product] = relationship(back_populates="sales_tracking_days")
