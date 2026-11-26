
"""Read-only demand forecasting and restock recommendations."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product
from app.services import demand_forecast_history


DEFAULT_LEAD_TIME_DAYS = 7
DEFAULT_SAFETY_STOCK_DAYS = 7


@dataclass(frozen=True)
class RestockRecommendation:
    product_id: int
    sku: str
    name: str
    current_stock: int
    minimum_stock: int

    tracked_days: int
    daily_demand: Decimal | None
    demand_7_days: int | None
    demand_30_days: int | None

    days_of_stock: Decimal | None
    reorder_quantity: int | None
    risk: str
    forecast_risk: str | None

    @property
    def physical_inventory_status(self) -> str:
        if self.current_stock == 0:
            return "Out of stock"
        return "Low stock" if self.current_stock <= self.minimum_stock else "In stock"

    @property
    def forecast_available(self) -> bool:
        return self.daily_demand is not None

    @property
    def forecast_availability(self) -> str:
        return "Available" if self.forecast_available else "Insufficient data"


def round_up(value: Decimal) -> int:
    """Round fractional units upward to whole units."""
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def calculate_recommendation(
    product: Product,
    daily_demand: Decimal | None,
    tracked_days: int = 0,
    lead_time_days: int = DEFAULT_LEAD_TIME_DAYS,
    safety_stock_days: int = DEFAULT_SAFETY_STOCK_DAYS,
) -> RestockRecommendation:
    """Calculate a transparent, deterministic restock recommendation."""

    if lead_time_days < 0 or safety_stock_days < 0:
        raise ValueError("Planning days cannot be negative.")

    stock = product.stock_quantity
    minimum = product.minimum_stock

    if daily_demand is None:
        return RestockRecommendation(
            product_id=product.id,
            sku=product.sku,
            name=product.name,
            current_stock=stock,
            minimum_stock=minimum,
            tracked_days=tracked_days,
            daily_demand=None,
            demand_7_days=None,
            demand_30_days=None,
            days_of_stock=None,
            reorder_quantity=None,
            risk="Out of stock" if stock == 0 else "Insufficient data",
            forecast_risk=None,
        )

    daily = Decimal(str(daily_demand))

    if daily < 0:
        raise ValueError("Daily demand cannot be negative.")

    demand_7 = round_up(daily * Decimal(7))
    demand_30 = round_up(daily * Decimal(30))

    if daily == 0:
        days_of_stock = None
        risk = "Out of stock" if stock == 0 else ("Low stock" if stock <= minimum else "Healthy")
        forecast_risk = "No observed demand"
    else:
        days_of_stock = Decimal(stock) / daily
        forecast_risk = "High risk" if days_of_stock <= Decimal(lead_time_days) else "Low risk"

        if stock == 0:
            risk = "Out of stock"
        elif days_of_stock <= Decimal(lead_time_days):
            risk = "High risk"
        elif stock <= minimum:
            risk = "Low stock"
        else:
            risk = "Healthy"

    planning_days = lead_time_days + safety_stock_days

    target_stock = max(
        minimum,
        round_up(daily * Decimal(planning_days)),
    )

    reorder_quantity = max(0, target_stock - stock)

    return RestockRecommendation(
        product_id=product.id,
        sku=product.sku,
        name=product.name,
        current_stock=stock,
        minimum_stock=minimum,
        tracked_days=tracked_days,
        daily_demand=daily,
        demand_7_days=demand_7,
        demand_30_days=demand_30,
        days_of_stock=days_of_stock,
        reorder_quantity=reorder_quantity,
        risk=risk,
        forecast_risk=forecast_risk,
    )


def product_recommendation(
    db: Session, product: Product, as_of: date,
    lead_time_days: int = DEFAULT_LEAD_TIME_DAYS,
    safety_stock_days: int = DEFAULT_SAFETY_STOCK_DAYS,
) -> RestockRecommendation:
    """Shared report/snapshot path, retaining coverage counts below the threshold."""
    history = demand_forecast_history(db, product.id, as_of=as_of)
    return calculate_recommendation(
        product, history.forecast.daily_average if history.forecast else None,
        tracked_days=history.tracked_days, lead_time_days=lead_time_days,
        safety_stock_days=safety_stock_days,
    )


def summarize_recommendations(items: list[RestockRecommendation]) -> dict:
    """Physical buckets are exclusive; availability is an independent dimension."""
    out = sum(item.current_stock == 0 for item in items)
    low = sum(item.physical_inventory_status == "Low stock" for item in items)
    predicted = sum(item.current_stock > 0 and item.forecast_risk == "High risk" for item in items)
    return {
        "out_of_stock_count": out,
        "physical_low_stock_count": low,
        "in_stock_count": len(items) - out - low,
        "current_shortage_count": out + low,
        "predicted_shortage_count": predicted,
        "high_risk_count": out + predicted,
        "attention_count": sum(
            item.physical_inventory_status != "In stock" or item.forecast_risk == "High risk"
            for item in items
        ),
        "forecast_available_count": sum(item.forecast_available for item in items),
        "insufficient_data_count": sum(not item.forecast_available for item in items),
    }


def build_forecast_report(
    db: Session,
    as_of: date | None = None,
    lead_time_days: int = DEFAULT_LEAD_TIME_DAYS,
    safety_stock_days: int = DEFAULT_SAFETY_STOCK_DAYS,
) -> dict:
    """Build a read-only forecast report for all products."""

    if lead_time_days < 0 or safety_stock_days < 0:
        raise ValueError("Planning days cannot be negative.")

    today = as_of or date.today()

    products = db.scalars(
        select(Product).order_by(Product.name, Product.id)
    ).all()

    recommendations = []

    for product in products:
        recommendation = product_recommendation(
            db, product, today,
            lead_time_days=lead_time_days,
            safety_stock_days=safety_stock_days,
        )

        recommendations.append(recommendation)

    risk_priority = {
        "Out of stock": 0,
        "High risk": 1,
        "Low stock": 2,
        "Insufficient data": 3,
        "Healthy": 4,
    }

    recommendations.sort(
        key=lambda item: (
            risk_priority[item.risk],
            item.name.lower(),
        )
    )

    return {
        "as_of": today,
        "lead_time_days": lead_time_days,
        "safety_stock_days": safety_stock_days,
        "products": recommendations,
        "total_products": len(recommendations),
        **summarize_recommendations(recommendations),
        "recommended_units": sum(
            item.reorder_quantity or 0
            for item in recommendations
        ),
    }
