"""Historical ingestion contracts, using migrated temporary databases exclusively."""

from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, timedelta
from decimal import Decimal
import io
from pathlib import Path
import re
from threading import Barrier
from uuid import uuid4
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from itsdangerous.timed import TimestampSigner
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import sales_import as imports
from app.ai_service import build_inventory_snapshot
from app.database import get_db
from app.forecasting import build_forecast_report
from app.main import app
from app.models import (HistoricalSale, HistoricalSalesCoverage, InventoryMovement, Product,
                        SalesImportBatch, SalesRecord, SalesTrackingDay, User)
from app.schemas import SaleInput
from app.services import SalesOverlapError, daily_demand_summary, mark_sales_day_complete, record_sale, simple_demand_forecast
from conftest import add_test_admin, login_test_client, reset_login_limiter

PROJECT = Path(__file__).resolve().parents[1]
DAY = date.today() - timedelta(days=1)


def csv_bytes(rows=None, header=imports.CSV_COLUMNS, bom=False):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows if rows is not None else [["EXT-1", DAY.isoformat(), "SP-1", "3", "4.25"]])
    return (("\ufeff" if bom else "") + stream.getvalue()).encode("utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    database = tmp_path / "imports.db"
    url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config(str(PROJECT / "alembic.ini")), "head")
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    add_test_admin(factory)
    with factory() as db:
        db.add_all([Product(sku="SP-1", name="Test One", category="Test", unit_price=Decimal("99.00"), stock_quantity=20, minimum_stock=5),
                    Product(sku="SP-2", name="Test Two", category="Test", unit_price=Decimal("99.00"), stock_quantity=10, minimum_stock=2)])
        db.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def save(db, content=None, source="Archive", key=None):
    return imports.execute_import(db, csv_bytes() if content is None else content, "sales.csv", source, 1, key or str(uuid4()))


def counts(db):
    return {model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (HistoricalSale, SalesImportBatch, InventoryMovement, SalesRecord, HistoricalSalesCoverage)}


@pytest.mark.parametrize("bom", [False, True])
def test_valid_preview_is_read_only_and_decimal_safe(env, bom):
    content = csv_bytes([["EXT-1", str(DAY), "SP-1", "3", "0.10"], ["EXT-2", str(DAY - timedelta(days=1)), "SP-2", "2", "1.25"]], bom=bom)
    with env() as db:
        before = counts(db)
        preview = imports.preview_csv(db, content, "../../sales.csv", "Archive")
        assert preview.can_import
        assert preview.filename == "sales.csv"
        assert len(preview.rows) == preview.count("valid") == 2
        assert preview.total_quantity == 5
        assert preview.sales_value == Decimal("2.80")
        assert preview.product_count == 2
        assert preview.date_range == (DAY - timedelta(days=1), DAY)
        assert counts(db) == before
        assert not db.new and not db.dirty


def test_import_preserves_inventory_prices_provenance_and_no_movements(env):
    with env() as db:
        batch = save(db)
        item = db.scalar(select(HistoricalSale))
        assert batch.imported_rows == 1 and batch.duplicate_rows == 0
        assert batch.source == "Archive" and batch.user_id == 1 and len(batch.file_sha256) == 64
        assert item.import_batch_id == batch.id and item.source_line == 2
        assert item.unit_selling_price == Decimal("4.25")
        assert db.get(Product, 1).stock_quantity == 20
        assert db.get(Product, 1).unit_price == Decimal("99.00")
        assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 0
        assert db.scalar(select(func.count()).select_from(SalesRecord)) == 0
        assert db.scalar(select(func.count()).select_from(HistoricalSalesCoverage)) == 0


@pytest.mark.parametrize("field,value", [
    ("external_sale_id", ""), ("external_sale_id", "=formula"),
    ("sku", "UNKNOWN"), ("sku", " SP-1"), ("sku", ""),
    ("sale_date", "not-a-date"), ("sale_date", "2026-02-30"),
    ("sale_date", "20260101"), ("sale_date", date.today().isoformat()),
    ("sale_date", (date.today() + timedelta(days=1)).isoformat()),
    ("quantity", "0"), ("quantity", "-2"), ("quantity", "1.5"),
    ("quantity", "abc"), ("quantity", "2147483648"),
    ("unit_price", "-0.01"), ("unit_price", "NaN"), ("unit_price", "Infinity"),
    ("unit_price", "1.001"), ("unit_price", "100000000.00"), ("unit_price", ""),
])
def test_row_validation_blocks_entire_import(env, field, value):
    row = dict(zip(imports.CSV_COLUMNS, ["EXT-1", str(DAY), "SP-1", "3", "4.25"]))
    row[field] = value
    content = csv_bytes([list(row.values()), ["EXT-2", str(DAY), "SP-2", "1", "0"]])
    with env() as db:
        preview = imports.preview_csv(db, content, "test.csv", "Archive")
        assert not preview.can_import
        assert preview.count("invalid") == 1
        assert preview.rows[0].errors
        with pytest.raises(imports.SalesImportError):
            save(db, content)
        assert counts(db)["historical_sales"] == 0
        assert counts(db)["sales_import_batches"] == 0


@pytest.mark.parametrize("content", [
    b"", b"\xef\xbb\xbf", b"wrong,columns\n", b"\xff\xfe", b"a\x00b",
    b"external_sale_id,sale_date,sku,quantity,unit_price\n",
    b'external_sale_id,sale_date,sku,quantity,unit_price\n"unclosed',
    b"external_sale_id,sale_date,sku,quantity,unit_price\na,b,c\n",
    b"external_sale_id,sale_date,sku,quantity,unit_price\n\n",
    b"external_sale_id,sale_date,sku,quantity,quantity\n",
])
def test_empty_missing_columns_and_malformed_csv(env, content):
    with env() as db:
        preview = imports.preview_csv(db, content, "bad.csv", "Archive")
        assert not preview.can_import
        assert preview.file_errors or preview.count("invalid")


def test_limits_are_checked_before_database_queries(monkeypatch):
    class NoQuery:
        def __getattr__(self, name):
            raise AssertionError("No query should run for an oversized file")
    assert not imports.preview_csv(NoQuery(), b"x" * (imports.MAX_FILE_BYTES + 1), "large.csv", "Archive").can_import
    monkeypatch.setattr(imports, "MAX_ROWS", 2)
    content = csv_bytes([[str(i), str(DAY), "SP-1", "1", "1"] for i in range(3)])
    preview = imports.preview_csv(NoQuery(), content, "many.csv", "Archive")
    assert "row limit" in preview.file_errors[0]


def test_identical_duplicates_within_file_and_repeated_imports(env):
    content = csv_bytes([["EXT-1", str(DAY), "SP-1", "3", "4.25"]] * 2)
    with env() as db:
        preview = imports.preview_csv(db, content, "same.csv", "Archive")
        assert preview.count("valid") == 1 and preview.count("duplicate") == 1
        assert preview.total_quantity == 3
        first = save(db, content)
        second = save(db, content)
        assert first.imported_rows == 1 and first.duplicate_rows == 1
        assert second.imported_rows == 0 and second.duplicate_rows == 2
        assert counts(db)["historical_sales"] == 1


def test_conflicting_identifier_in_file_marks_both_rows(env):
    content = csv_bytes([["EXT-1", str(DAY), "SP-1", "3", "4.25"], ["EXT-1", str(DAY), "SP-1", "4", "4.25"]])
    with env() as db:
        preview = imports.preview_csv(db, content, "conflict.csv", "Archive")
        assert preview.count("conflict") == 2
        assert not preview.can_import


def test_existing_identifier_conflicts_and_changed_sources(env):
    with env() as db:
        save(db)
        changed = csv_bytes([["EXT-1", str(DAY), "SP-1", "4", "4.25"]])
        assert imports.preview_csv(db, changed, "changed.csv", "Archive").count("conflict") == 1
        assert imports.preview_csv(db, csv_bytes(), "changed.csv", "Another source").count("conflict") == 1
        other_id = csv_bytes([["OTHER-ID", str(DAY), "SP-1", "3", "4.25"]])
        assert imports.preview_csv(db, other_id, "changed.csv", "Another source").count("conflict") == 1


def test_confirmation_key_is_idempotent_and_cannot_change_payload(env):
    key = str(uuid4())
    with env() as db:
        first = save(db, key=key)
        second = save(db, key=key)
        assert first.id == second.id
        assert counts(db)["sales_import_batches"] == 1
        with pytest.raises(imports.SalesImportError):
            save(db, csv_bytes([["EXT-2", str(DAY), "SP-1", "3", "4.25"]]), key=key)


def test_commit_failure_rolls_back_batch_rows_and_inventory(env, monkeypatch):
    with env() as db:
        monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("SECRET_DATABASE_FAILURE")))
        with pytest.raises(imports.SalesImportError) as caught:
            save(db)
        assert "SECRET" not in str(caught.value)
    with env() as db:
        assert counts(db)["historical_sales"] == counts(db)["sales_import_batches"] == 0
        assert db.get(Product, 1).stock_quantity == 20


def test_failure_after_historical_inserts_rolls_back_every_record(env):
    inserted = []

    def fail_after_insert(mapper, connection, target):
        assert connection.scalar(select(func.count()).select_from(HistoricalSale)) > 0
        inserted.append(target.external_sale_id)
        raise RuntimeError("SECRET_FLUSH_FAILURE")

    content = csv_bytes([["EXT-1", str(DAY), "SP-1", "3", "4.25"], ["EXT-2", str(DAY), "SP-2", "1", "2.00"]])
    event.listen(HistoricalSale, "after_insert", fail_after_insert)
    try:
        with env() as db:
            with pytest.raises(imports.SalesImportError) as caught:
                save(db, content)
            assert "SECRET" not in str(caught.value)
    finally:
        event.remove(HistoricalSale, "after_insert", fail_after_insert)
    assert inserted
    with env() as db:
        assert counts(db)["historical_sales"] == counts(db)["sales_import_batches"] == 0
        assert db.get(Product, 1).stock_quantity == 20


@pytest.mark.parametrize("same_confirmation", [False, True])
def test_concurrent_duplicate_imports(env, same_confirmation):
    barrier = Barrier(2)
    common = str(uuid4())

    def worker(index):
        with env() as db:
            barrier.wait()
            batch = save(db, key=common if same_confirmation else str(uuid4()))
            return batch.imported_rows

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(worker, range(2)))
    with env() as db:
        assert counts(db)["historical_sales"] == 1
        assert counts(db)["sales_import_batches"] == (1 if same_confirmation else 2)
        assert db.get(Product, 1).stock_quantity == 20


@pytest.mark.parametrize("coverage", [False, True])
def test_operational_overlap_rejects_import(env, coverage):
    with env() as db:
        if coverage:
            mark_sales_day_complete(db, 1, DAY)
        else:
            record_sale(db, SaleInput(product_id=1, sale_date=DAY, quantity_sold=3, idempotency_key=uuid4()))
        assert imports.preview_csv(db, csv_bytes(), "overlap.csv", "Archive").count("conflict") == 1
        with pytest.raises(imports.SalesImportError):
            save(db)


@pytest.mark.parametrize("zero_day", [False, True])
def test_historical_ownership_blocks_operational_sales_and_coverage(env, zero_day):
    with env() as db:
        if zero_day:
            imports.declare_historical_coverage(db, 1, DAY, "Archive", "Verified original daily report", 1)
        else:
            save(db)
        with pytest.raises(SalesOverlapError):
            record_sale(db, SaleInput(product_id=1, sale_date=DAY, quantity_sold=3, idempotency_key=uuid4()))
        with pytest.raises(SalesOverlapError):
            mark_sales_day_complete(db, 1, DAY)
        assert db.get(Product, 1).stock_quantity == 20


def test_database_trigger_rejects_direct_operational_insert(env):
    with env() as db:
        save(db)
        db.add(SalesRecord(product_id=1, sale_date=DAY, quantity_sold=1, unit_selling_price=Decimal("1")))
        with pytest.raises(IntegrityError, match="historical_operational_overlap"):
            db.commit()
        db.rollback()


def test_database_trigger_rejects_reverse_overlap(env):
    with env() as db:
        batch = save(db)
        older = DAY - timedelta(days=1)
        record_sale(db, SaleInput(product_id=1, sale_date=older, quantity_sold=1, idempotency_key=uuid4()))
        db.add(HistoricalSale(import_batch_id=batch.id, external_sale_id="OTHER", product_id=1,
                              sale_date=older, quantity_sold=1, unit_selling_price=Decimal("1"), source_line=2))
        with pytest.raises(IntegrityError, match="historical_operational_overlap"):
            db.commit()
        db.rollback()


def test_concurrent_operational_sale_and_import_cannot_overlap(env):
    barrier = Barrier(2)

    def operational():
        with env() as db:
            barrier.wait()
            try:
                record_sale(db, SaleInput(product_id=1, sale_date=DAY, quantity_sold=3, idempotency_key=uuid4()))
                return "operational"
            except SalesOverlapError:
                return "rejected"

    def historical():
        with env() as db:
            barrier.wait()
            try:
                save(db)
                return "historical"
            except imports.SalesImportError:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as workers:
        tasks = [workers.submit(operational), workers.submit(historical)]
        outcomes = [task.result() for task in tasks]
    with env() as db:
        c = counts(db)
        assert c["sales_records"] + c["historical_sales"] == 1
        assert outcomes.count("rejected") == 1
        assert db.get(Product, 1).stock_quantity == (17 if c["sales_records"] else 20)
        assert c["inventory_movements"] == c["sales_records"]


def test_database_unique_external_identifier(env):
    with env() as db:
        batch = save(db)
        db.add(HistoricalSale(import_batch_id=batch.id, external_sale_id="EXT-1", product_id=2,
                              sale_date=DAY, quantity_sold=1, unit_selling_price=Decimal("1"), source_line=2))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_historical_coverage_is_explicit_unknown_and_zero_are_distinct(env):
    with env() as db:
        save(db)
        start = DAY - timedelta(days=2)
        summary = daily_demand_summary(db, 1, start, DAY)
        assert [p.demand_quantity for p in summary] == [None, None, None]
        assert summary[-1].recorded_quantity == 3
        assert imports.declare_historical_coverage(db, 1, start, "Archive", "Verified zero units sold", 1)
        assert imports.declare_historical_coverage(db, 1, DAY, "Archive", "Verified all daily sales", 1)
        assert not imports.declare_historical_coverage(db, 1, DAY, "Archive", "Verified all daily sales", 1)
        summary = daily_demand_summary(db, 1, start, DAY)
        assert [p.demand_quantity for p in summary] == [0, None, 3]
        assert db.get(Product, 1).stock_quantity == 20


def test_completed_history_and_operational_days_contribute_without_formula_changes(env):
    today = date.today()
    content = csv_bytes([[f"OLD-{i}", str(today - timedelta(days=i)), "SP-1", "2", "4.25"] for i in range(3, 8)])
    with env() as db:
        save(db, content)
        assert simple_demand_forecast(db, 1) is None
        for i in range(3, 8):
            imports.declare_historical_coverage(db, 1, today - timedelta(days=i), "Archive", "Verified full POS day", 1)
        for i in (1, 2):
            record_sale(db, SaleInput(product_id=1, sale_date=today - timedelta(days=i), quantity_sold=2,
                                      mark_day_complete=True, idempotency_key=uuid4()))
        forecast = simple_demand_forecast(db, 1)
        assert forecast.tracked_days == 7 and forecast.daily_average == Decimal("2")
        assert forecast.weekly_estimate == Decimal("14")
        recommendation = next(item for item in build_forecast_report(db)["products"] if item.product_id == 1)
        assert recommendation.reorder_quantity == 12
        assert recommendation.current_stock == 16
        assert build_inventory_snapshot(db)["products"][0]["daily_demand_estimate"] == 2.0


def test_completed_days_cannot_accept_new_history_and_source_must_match(env):
    with env() as db:
        save(db)
        with pytest.raises(imports.SalesImportError, match="source"):
            imports.declare_historical_coverage(db, 1, DAY, "Different", "Verified daily report", 1)
        imports.declare_historical_coverage(db, 1, DAY, "Archive", "Verified daily report", 1)
        extra = csv_bytes([["EXTRA", str(DAY), "SP-1", "1", "4.25"]])
        assert imports.preview_csv(db, extra, "extra.csv", "Archive").count("conflict") == 1
        assert imports.preview_csv(db, csv_bytes(), "same.csv", "Archive").count("duplicate") == 1


def test_import_history_window_and_six_seven_day_threshold(env):
    today = date.today()
    days = [1, 2, 3, 4, 5, 6, 28, 29]
    content = csv_bytes([[f"BOUNDARY-{i}", str(today - timedelta(days=i)), "SP-1", "2", "4.25"] for i in days])
    with env() as db:
        save(db, content)
        for i in [1, 2, 3, 4, 5, 6, 29]:
            imports.declare_historical_coverage(db, 1, today - timedelta(days=i), "Archive", "Verified daily report", 1)
        assert simple_demand_forecast(db, 1) is None  # Day 29 cannot unlock the threshold.
        imports.declare_historical_coverage(db, 1, today - timedelta(days=28), "Archive", "Verified daily report", 1)
        forecast = simple_demand_forecast(db, 1)
        assert forecast.tracked_days == 7 and forecast.daily_average == Decimal("2")
        assert db.get(Product, 1).stock_quantity == 20


@pytest.mark.parametrize("active,admin", [(False, True), (True, False)])
def test_service_rejects_inactive_and_non_admin_users(env, active, admin):
    with env() as db:
        user = db.get(User, 1)
        user.is_active, user.is_admin = active, admin
        db.commit()
        with pytest.raises(imports.SalesImportError, match="administrator"):
            save(db)
        assert counts(db)["historical_sales"] == counts(db)["sales_import_batches"] == 0


def test_reordered_csv_columns_are_supported(env):
    content = csv_bytes([["SP-1", "4.25", "3", str(DAY), "EXT-1"]],
                        header=["sku", "unit_price", "quantity", "sale_date", "external_sale_id"])
    with env() as db:
        assert save(db, content).imported_rows == 1


@pytest.fixture
def client(env):
    def override():
        with env() as db:
            yield db
    app.dependency_overrides[get_db] = override
    reset_login_limiter()
    try:
        with TestClient(app, base_url="https://testserver") as browser:
            yield browser
    finally:
        app.dependency_overrides.clear()
        reset_login_limiter()


def upload(client, content=None, name="sales.csv", source="Archive"):
    return client.post("/sales/import/preview", data={"source": source},
                       files={"file": (name, csv_bytes() if content is None else content, "text/csv")})


def token_from(response):
    return re.search(r'name="preview_token" value="([^"]+)"', response.text).group(1)


def test_authenticated_preview_confirmation_and_history_page(client, env):
    login_test_client(client)
    assert "Historical Sales Import" in client.get("/sales").text
    template = client.get("/sales/import/template")
    assert template.status_code == 200 and template.content.startswith(b"\xef\xbb\xbfexternal_sale_id")
    response = upload(client)
    assert response.status_code == 200
    assert "Confirm historical import" in response.text
    with env() as db:
        assert counts(db)["historical_sales"] == counts(db)["sales_import_batches"] == 0
    confirmed = client.post("/sales/import/confirm", data={"preview_token": token_from(response)})
    assert confirmed.status_code == 200 and "Historical import saved" in confirmed.text
    assert "Archive" in client.get("/sales/import").text
    assert "EXT-1" in client.get("/sales/import").text
    assert response.headers["cache-control"] == "no-store"
    with env() as db:
        assert counts(db)["historical_sales"] == 1


def test_large_monetary_preview_renders_without_float_rounding(client):
    login_test_client(client)
    content = csv_bytes([["LARGE", str(DAY), "SP-1", "2147483647", "99999999.99"]])
    response = upload(client, content)
    assert response.status_code == 200
    assert "214748364678525163.53" in response.text


def test_confirm_uses_signed_source_instead_of_injected_fields(client, env):
    login_test_client(client)
    token = token_from(upload(client))
    response = client.post("/sales/import/confirm", data={"preview_token": token, "source": "Injected"})
    assert response.status_code == 200
    with env() as db:
        assert db.scalar(select(SalesImportBatch.source)) == "Archive"


@pytest.mark.parametrize("path", ["/sales/import", "/sales/import/template"])
def test_import_get_requires_authentication(client, path):
    assert client.get(path, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("path", ["preview", "confirm", "coverage"])
def test_import_posts_require_authentication_and_csrf(client, path):
    assert client.post(f"/sales/import/{path}", follow_redirects=False).status_code == 303
    login_test_client(client)
    client.headers.pop("X-CSRF-Token")
    assert client.post(f"/sales/import/{path}").status_code == 403
    client.headers["X-CSRF-Token"] = "invalid-token"
    assert client.post(f"/sales/import/{path}").status_code == 403


def test_tampered_and_replayed_preview_are_rejected(client, env):
    login_test_client(client)
    preview = upload(client)
    token = token_from(preview)
    response = client.post("/sales/import/confirm", data={"preview_token": token + "tampered"})
    assert response.status_code == 400
    with env() as db:
        assert counts(db)["historical_sales"] == 0
    preview = upload(client)
    token = token_from(preview)
    assert client.post("/sales/import/confirm", data={"preview_token": token}).status_code == 200
    assert client.post("/sales/import/confirm", data={"preview_token": token}).status_code == 400


def test_new_preview_invalidates_previous_token(client):
    login_test_client(client)
    old_token = token_from(upload(client))
    upload(client)
    assert client.post("/sales/import/confirm", data={"preview_token": old_token}).status_code == 400


def test_oversized_upload_has_no_confirmation(client, env):
    login_test_client(client)
    response = upload(client, b"x" * (imports.MAX_FILE_BYTES + 1))
    assert response.status_code == 200
    assert "at most 1 MiB" in response.text
    assert "Confirm historical import" not in response.text
    with env() as db:
        assert counts(db)["historical_sales"] == 0


def test_unknown_sku_added_after_preview_cannot_be_substituted(client, env):
    login_test_client(client)
    token = token_from(upload(client))
    with env() as db:
        db.get(Product, 1).sku = "RENAMED"
        db.commit()
    response = client.post("/sales/import/confirm", data={"preview_token": token})
    assert response.status_code == 400
    with env() as db:
        assert counts(db)["historical_sales"] == 0


def test_preview_is_user_session_bound_and_expires():
    nonce = str(uuid4())
    token = imports.sign_preview(csv_bytes(), "test.csv", "Archive", 1, nonce)
    for user, session_nonce in ((2, nonce), (1, str(uuid4())), (1, None)):
        with pytest.raises(imports.SalesImportError, match="invalid or expired"):
            imports.read_preview(token, user, session_nonce)
    with patch.object(TimestampSigner, "get_timestamp", return_value=1):
        expired = imports.sign_preview(csv_bytes(), "test.csv", "Archive", 1, nonce)
    with pytest.raises(imports.SalesImportError, match="invalid or expired"):
        imports.read_preview(expired, 1, nonce)


def test_confirmation_revalidates_new_operational_overlap(client, env):
    login_test_client(client)
    token = token_from(upload(client))
    with env() as db:
        record_sale(db, SaleInput(product_id=1, sale_date=DAY, quantity_sold=2, idempotency_key=uuid4()))
    response = client.post("/sales/import/confirm", data={"preview_token": token})
    assert response.status_code == 400 and "revalidation" in response.text
    with env() as db:
        assert counts(db)["historical_sales"] == 0
        assert db.get(Product, 1).stock_quantity == 18


def test_uploaded_values_and_filename_are_html_escaped(client):
    login_test_client(client)
    body = csv_bytes([["EXT-1", str(DAY), "<script>alert(1)</script>", "3", "4.25"]])
    response = upload(client, body, "../../<script>.csv")
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "Confirm historical import" not in response.text


def test_body_limit_enforced_on_actual_bytes(client):
    login_test_client(client)
    response = client.post("/sales/import/preview", content=b"x" * (imports.MAX_BODY_BYTES + 1),
                           headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 413


def test_coverage_http_workflow(client, env):
    login_test_client(client)
    response = client.post("/sales/import/coverage", data={"product_id": 1, "sales_date": str(DAY),
                                                          "source": "Archive", "reason": "Verified zero sales"})
    assert response.status_code == 200 and "marked complete" in response.text
    with env() as db:
        assert daily_demand_summary(db, 1, DAY, DAY)[0].demand_quantity == 0


def test_upgrade_from_phase53_preserves_existing_records(tmp_path, monkeypatch):
    database = tmp_path / "prior.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    cfg = Config(str(PROJECT / "alembic.ini"))
    command.upgrade(cfg, "20261126_53")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with engine.begin() as db:
            db.execute(text("INSERT INTO products VALUES (1,'KEPT','Kept','Test',4.25,10,2,CURRENT_TIMESTAMP)"))
            before = db.execute(text("SELECT * FROM products")).all()
        command.upgrade(cfg, "head")
        command.upgrade(cfg, "head")
        with engine.connect() as db:
            assert db.execute(text("SELECT * FROM products")).all() == before
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261126_6"
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
            assert db.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    finally:
        engine.dispose()


def test_backup_preserves_imports_coverage_and_overlap_guards(env):
    from app import backup_db
    with env() as db:
        save(db)
        imports.declare_historical_coverage(db, 1, DAY, "Archive", "Verified daily report", 1)
    backup = backup_db.main()
    engine = create_engine(f"sqlite:///{backup.as_posix()}")
    try:
        with engine.connect() as db:
            assert db.scalar(select(func.count()).select_from(HistoricalSale)) == 1
            assert db.scalar(select(func.count()).select_from(SalesImportBatch)) == 1
            assert db.scalar(select(func.count()).select_from(HistoricalSalesCoverage)) == 1
            assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert db.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            with pytest.raises(IntegrityError, match="historical_operational_overlap"):
                db.execute(text("INSERT INTO sales_records(product_id,sale_date,quantity_sold,unit_selling_price) VALUES (1,:day,1,1)"), {"day": DAY.isoformat()})
            db.rollback()
    finally:
        engine.dispose()
