
"""Read-only demand forecasting and restock recommendations."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product
from app.services import simple_demand_forecast


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
            risk="Insufficient data",
        )

    daily = Decimal(str(daily_demand))

    if daily < 0:
        raise ValueError("Daily demand cannot be negative.")

    demand_7 = round_up(daily * Decimal(7))
    demand_30 = round_up(daily * Decimal(30))

    if daily == 0:
        days_of_stock = None
        risk = "Low stock" if stock <= minimum else "Healthy"
    else:
        days_of_stock = Decimal(stock) / daily

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
    )


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
        forecast = simple_demand_forecast(
            db,
            product.id,
            as_of=today,
        )

        recommendation = calculate_recommendation(
            product=product,
            daily_demand=(
                forecast.daily_average if forecast else None
            ),
            tracked_days=(
                forecast.tracked_days if forecast else 0
            ),
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
        "high_risk_count": sum(
            item.risk in ("Out of stock", "High risk")
            for item in recommendations
        ),
        "insufficient_data_count": sum(
            item.risk == "Insufficient data"
            for item in recommendations
        ),
        "recommended_units": sum(
            item.reorder_quantity or 0
            for item in recommendations
        ),
    }
