
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.forecasting import build_forecast_report
from app.models import Product, SalesRecord, SalesTrackingDay


def test_forecast_with_seven_days_of_sales():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(engine)

    try:
        with Session(engine) as db:
            product = Product(
                sku="FORECAST-TEST-001",
                name="Forecast Test Product",
                category="Testing",
                unit_price=Decimal("100.00"),
                stock_quantity=10,
                minimum_stock=5,
            )

            db.add(product)
            db.flush()

            today = date(2026, 11, 19)

            for days_ago in range(1, 8):
                sale_date = today - timedelta(days=days_ago)

                db.add(
                    SalesRecord(
                        product_id=product.id,
                        sale_date=sale_date,
                        quantity_sold=2,
                        unit_selling_price=Decimal("100.00"),
                    )
                )

                db.add(
                    SalesTrackingDay(
                        product_id=product.id,
                        sales_date=sale_date,
                    )
                )

            db.commit()

            report = build_forecast_report(
                db,
                as_of=today,
            )

            assert report["total_products"] == 1
            assert report["high_risk_count"] == 1
            assert report["recommended_units"] == 18

            item = report["products"][0]

            assert item.daily_demand == Decimal("2")
            assert item.demand_7_days == 14
            assert item.demand_30_days == 60
            assert item.days_of_stock == Decimal("5")
            assert item.reorder_quantity == 18
            assert item.risk == "High risk"
            assert item.tracked_days == 7

    finally:
        engine.dispose()
