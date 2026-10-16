from decimal import Decimal

from sqlalchemy import select

from app.database import SessionLocal
from app.init_db import main as migrate_database
from app.models import InventoryMovement, Product

SAMPLES = [
    ("SP-1001", "Wireless Mouse", "Peripherals", "59.90", 32, 8),
    ("SP-1002", "Mechanical Keyboard", "Peripherals", "249.00", 18, 5),
    ("SP-1003", "USB-C Hub", "Accessories", "129.90", 4, 6),
    ("SP-1004", "Laptop Stand", "Accessories", "89.00", 12, 4),
    ("SP-1005", "Webcam 1080p", "Peripherals", "179.00", 2, 5),
]


def main():
    migrate_database()
    with SessionLocal() as db:
        if db.scalar(select(Product.id).limit(1)) is not None:
            print("Products already exist; no sample data added.")
            return
        for sku, name, category, price, quantity, minimum in SAMPLES:
            product = Product(sku=sku, name=name, category=category, unit_price=Decimal(price),
                              stock_quantity=quantity, minimum_stock=minimum)
            db.add(product)
            db.flush()
            if quantity:
                db.add(InventoryMovement(product_id=product.id, quantity=quantity, reason="Sample opening stock"))
        db.commit()
    print("Sample products added.")


if __name__ == "__main__":
    main()
