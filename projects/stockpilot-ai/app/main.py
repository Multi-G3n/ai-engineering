from datetime import date, timedelta
from pathlib import Path
import secrets
from uuid import uuid4
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import InventoryMovement, Product, SalesRecord
from app.config import session_cookie_secure, session_secret_key
from app.schemas import AdjustmentInput, ProductInput, SaleInput, SalesTrackingInput
from app.services import (
    DuplicateSKUError,
    DuplicateSaleError,
    DemandRangeError,
    InsufficientStockError,
    InvalidAdjustmentError,
    ProductNotFoundError,
    adjust_stock,
    create_product,
    daily_demand_summary,
    dashboard_stats,
    mark_sales_day_complete,
    record_sale,
    update_product,
)
from app.security import authenticate_user, require_authenticated_user, require_csrf

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="StockPilot AI", description="Inventory management", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret_key(),
    session_cookie="stockpilot_session",
    max_age=8 * 60 * 60,
    same_site="lax",
    https_only=session_cookie_secure(),
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


def render(request: Request, page: str, **context):
    csrf_token = request.session.get("csrf_token")
    if not csrf_token:
        csrf_token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = csrf_token
    context.update(
        csrf_token=csrf_token,
        is_authenticated=bool(request.session.get("user_id")),
        display_username=request.session.get("username", ""),
    )
    status_code = context.pop("status_code", 200)
    return templates.TemplateResponse(request=request, name=page, context=context,
                                      status_code=status_code)


def product_form_data(sku, name, category, unit_price, stock_quantity, minimum_stock):
    return ProductInput(sku=sku, name=name, category=category, unit_price=unit_price,
                        stock_quantity=stock_quantity, minimum_stock=minimum_stock)


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_authenticated_user)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    stats = dashboard_stats(db)
    low_stock = db.scalars(select(Product).where(Product.stock_quantity <= Product.minimum_stock)
                           .order_by(Product.stock_quantity, Product.name).limit(6)).all()
    recent = db.scalars(select(InventoryMovement).order_by(InventoryMovement.created_at.desc()).limit(6)).all()
    return render(request, "dashboard.html", stats=stats, low_stock=low_stock, recent=recent)


@app.get("/products", response_class=HTMLResponse, dependencies=[Depends(require_authenticated_user)])
def products(request: Request, search: str = "", status: str = "all", db: Session = Depends(get_db)):
    query = select(Product)
    if search.strip():
        term = f"%{search.strip()}%"
        query = query.where(or_(Product.name.ilike(term), Product.sku.ilike(term), Product.category.ilike(term)))
    if status == "low":
        query = query.where(Product.stock_quantity <= Product.minimum_stock)
    elif status == "in_stock":
        query = query.where(Product.stock_quantity > Product.minimum_stock)
    items = db.scalars(query.order_by(Product.name)).all()
    return render(request, "products.html", products=items, search=search, status=status)


@app.get("/products/new", response_class=HTMLResponse, dependencies=[Depends(require_authenticated_user)])
def new_product(request: Request):
    return render(request, "product_form.html", product=None, error=None)


@app.post("/products/new", response_class=HTMLResponse,
          dependencies=[Depends(require_authenticated_user), Depends(require_csrf)])
def create_product_route(request: Request, sku: str = Form(...), name: str = Form(...), category: str = Form(...),
                         unit_price: str = Form(...), stock_quantity: str = Form(...), minimum_stock: str = Form(...),
                         db: Session = Depends(get_db)):
    try:
        data = product_form_data(sku, name, category, unit_price, stock_quantity, minimum_stock)
        create_product(db, data)
    except ValidationError as exc:
        return render(request, "product_form.html", product=None, error=" ".join(e["msg"] for e in exc.errors()))
    except DuplicateSKUError as exc:
        return render(request, "product_form.html", product=None, error=str(exc))
    return RedirectResponse("/products", status_code=303)


@app.get("/products/{product_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_authenticated_user)])
def edit_product(request: Request, product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        return RedirectResponse("/products", status_code=303)
    return render(request, "product_form.html", product=product, error=None)


@app.post("/products/{product_id}/edit", response_class=HTMLResponse,
          dependencies=[Depends(require_authenticated_user), Depends(require_csrf)])
def update_product_route(request: Request, product_id: int, sku: str = Form(...), name: str = Form(...),
                         category: str = Form(...), unit_price: str = Form(...), stock_quantity: str = Form(...),
                         minimum_stock: str = Form(...), db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        return RedirectResponse("/products", status_code=303)
    try:
        data = product_form_data(sku, name, category, unit_price, stock_quantity, minimum_stock)
        update_product(db, product, data)
    except ValidationError as exc:
        return render(request, "product_form.html", product=product, error=" ".join(e["msg"] for e in exc.errors()))
    except DuplicateSKUError as exc:
        return render(request, "product_form.html", product=product, error=str(exc))
    return RedirectResponse("/products", status_code=303)


@app.get("/products/{product_id}/stock", response_class=HTMLResponse, dependencies=[Depends(require_authenticated_user)])
def stock_page(request: Request, product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        return RedirectResponse("/products", status_code=303)
    movements = db.scalars(select(InventoryMovement).where(InventoryMovement.product_id == product_id)
                           .order_by(InventoryMovement.created_at.desc())).all()
    return render(request, "stock.html", product=product, movements=movements, error=None)


@app.post("/products/{product_id}/stock", response_class=HTMLResponse,
          dependencies=[Depends(require_authenticated_user), Depends(require_csrf)])
def stock_adjust_route(request: Request, product_id: int, direction: str = Form(...), quantity: str = Form(...),
                       reason: str = Form(...), db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        return RedirectResponse("/products", status_code=303)
    try:
        data = AdjustmentInput(direction=direction, quantity=quantity, reason=reason)
        adjust_stock(db, product_id, data)
        return RedirectResponse(f"/products/{product_id}/stock", status_code=303)
    except ValidationError as exc:
        error = " ".join(e["msg"] for e in exc.errors())
    except InvalidAdjustmentError as exc:
        error = str(exc)
    movements = db.scalars(select(InventoryMovement).where(InventoryMovement.product_id == product_id)
                           .order_by(InventoryMovement.created_at.desc())).all()
    return render(request, "stock.html", product=product, movements=movements, error=error)


@app.get("/movements", response_class=HTMLResponse, dependencies=[Depends(require_authenticated_user)])
def movement_history(request: Request, db: Session = Depends(get_db)):
    movements = db.scalars(select(InventoryMovement).order_by(InventoryMovement.created_at.desc()).limit(100)).all()
    return render(request, "movements.html", movements=movements)


def sales_context(request: Request, db: Session, product_id: int | None, start_date: date,
                  end_date: date, sale_error: str | None = None, tracking_error: str | None = None,
                  form_values: dict | None = None):
    available_products = db.scalars(select(Product).order_by(Product.name)).all()
    selected_product = db.get(Product, product_id) if product_id is not None else (
        available_products[0] if available_products else None
    )
    summary = []
    range_error = None
    recent_sales = []
    if end_date < start_date:
        range_error = "End date must be on or after start date."
    elif selected_product is not None:
        try:
            summary = daily_demand_summary(db, selected_product.id, start_date, end_date)
        except DemandRangeError as exc:
            range_error = str(exc)
        recent_sales = db.scalars(
            select(SalesRecord)
            .where(SalesRecord.product_id == selected_product.id)
            .order_by(SalesRecord.sale_date.desc(), SalesRecord.created_at.desc())
            .limit(20)
        ).all()
    return render(
        request,
        "sales.html",
        products=available_products,
        selected_product=selected_product,
        start_date=start_date,
        end_date=end_date,
        summary=summary,
        recent_sales=recent_sales,
        sale_error=sale_error,
        tracking_error=tracking_error,
        range_error=range_error,
        form_values={"idempotency_key": str(uuid4()), **(form_values or {})},
    )


@app.get("/sales", response_class=HTMLResponse, dependencies=[Depends(require_authenticated_user)])
def sales_page(request: Request, product_id: int | None = None, start_date: date | None = None,
               end_date: date | None = None, db: Session = Depends(get_db)):
    today = date.today()
    return sales_context(request, db, product_id, start_date or today - timedelta(days=13), end_date or today)


@app.post("/sales", response_class=HTMLResponse,
          dependencies=[Depends(require_authenticated_user), Depends(require_csrf)])
def create_sale_route(request: Request, product_id: str = Form(...), quantity_sold: str = Form(...),
                      sale_date: str = Form(...), unit_selling_price: str = Form(""),
                      mark_day_complete: bool = Form(False), idempotency_key: str = Form(...),
                      db: Session = Depends(get_db)):
    form_values = {"product_id": product_id, "quantity_sold": quantity_sold,
                   "sale_date": sale_date, "unit_selling_price": unit_selling_price,
                   "mark_day_complete": mark_day_complete, "idempotency_key": idempotency_key}
    try:
        data = SaleInput(product_id=product_id, quantity_sold=quantity_sold, sale_date=sale_date,
                         unit_selling_price=unit_selling_price, mark_day_complete=mark_day_complete,
                         idempotency_key=idempotency_key)
        record_sale(db, data)
    except ValidationError as exc:
        error = " ".join(item["msg"] for item in exc.errors())
        product_value = form_values.get("product_id")
        selected_id = int(product_value) if product_value.isdigit() else None
        today = date.today()
        return sales_context(request, db, selected_id, today - timedelta(days=13), today,
                             sale_error=error, form_values=form_values)
    except (InsufficientStockError, ProductNotFoundError, DuplicateSaleError) as exc:
        today = date.today()
        product_value = form_values.get("product_id")
        selected_id = int(product_value) if product_value.isdigit() else None
        return sales_context(request, db, selected_id, today - timedelta(days=13), today,
                             sale_error=str(exc), form_values=form_values)
    query = urlencode({"product_id": data.product_id, "start_date": data.sale_date.isoformat(),
                       "end_date": data.sale_date.isoformat()})
    return RedirectResponse(f"/sales?{query}", status_code=303)


@app.post("/sales/coverage", response_class=HTMLResponse,
          dependencies=[Depends(require_authenticated_user), Depends(require_csrf)])
def mark_sales_coverage_route(request: Request, product_id: str = Form(...), sales_date: str = Form(...),
                              db: Session = Depends(get_db)):
    try:
        data = SalesTrackingInput(product_id=product_id, sales_date=sales_date)
        mark_sales_day_complete(db, data.product_id, data.sales_date)
    except ValidationError as exc:
        error = " ".join(item["msg"] for item in exc.errors())
        selected_id = int(product_id) if product_id.isdigit() else None
        today = date.today()
        return sales_context(request, db, selected_id, today - timedelta(days=13), today, tracking_error=error)
    except ProductNotFoundError as exc:
        today = date.today()
        selected_id = int(product_id) if product_id.isdigit() else None
        return sales_context(request, db, selected_id, today - timedelta(days=13), today, tracking_error=str(exc))
    query = urlencode({"product_id": data.product_id, "start_date": data.sales_date.isoformat(),
                       "end_date": data.sales_date.isoformat()})
    return RedirectResponse(f"/sales?{query}", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error=None)


@app.post("/login", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def login(request: Request, username: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    client_address = request.client.host if request.client else "unknown"
    user = authenticate_user(db, username, password, client_address)
    if user is None:
        return render(request, "login.html", error="Invalid username or password.", status_code=401)
    request.session.clear()
    request.session.update({
        "user_id": user.id,
        "username": user.username,
        "csrf_token": secrets.token_urlsafe(32),
    })
    return RedirectResponse("/", status_code=303)


@app.post("/logout", response_class=HTMLResponse,
          dependencies=[Depends(require_authenticated_user), Depends(require_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
