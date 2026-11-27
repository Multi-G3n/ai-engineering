import copy
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (Product, User, SalesRecord, SalesTrackingDay, HistoricalSale,
                        HistoricalSalesCoverage, SalesImportBatch)
from app.data_pipeline import generate, DataQualityError
from app.data_pipeline.extraction import extract
from app.data_pipeline.features import engineer
from app.data_pipeline.validation import validate_sources, quality_report
from app.data_pipeline.export import export_dataset

START = date(2026, 1, 1)
AS_OF = date(2026, 2, 15)


@pytest.fixture
def source(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'synthetic.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Product(id=i, sku=f'SYN-{i}', name='Synthetic', unit_price=Decimal('99'),
                            stock_quantity=50, created_at=datetime(2026, 1, 1)) for i in (1, 2)])
        db.add(User(id=1, username='synthetic', password_hash='unused'))
        db.flush()
        db.add(SalesImportBatch(id=1, confirmation_key='synthetic', source='archive', filename='unused',
                                file_sha256='0'*64, user_id=1, total_rows=1, imported_rows=1, duplicate_rows=0))
        db.flush()
        for n in range(35):
            day = START + timedelta(days=n)
            db.add(SalesTrackingDay(product_id=1, sales_date=day))
            db.add(SalesRecord(product_id=1, sale_date=day, quantity_sold=n+1, unit_selling_price=Decimal('.10')))
        db.add(SalesRecord(product_id=1, sale_date=START, quantity_sold=2, unit_selling_price=Decimal('.20')))
        db.add(HistoricalSale(product_id=2, sale_date=START, quantity_sold=3,
                              unit_selling_price=Decimal('.10'), import_batch_id=1,
                              external_sale_id='synth/1', source_line=2))
        db.add(HistoricalSalesCoverage(product_id=2, sales_date=START, source='archive', user_id=1, reason='synthetic'))
        db.add(HistoricalSalesCoverage(product_id=2, sales_date=START+timedelta(days=1), source='archive', user_id=1, reason='zero'))
        db.add(SalesRecord(product_id=2, sale_date=START+timedelta(days=3), quantity_sold=9, unit_selling_price=Decimal('.10')))
        db.commit()
    yield engine
    engine.dispose()


def build(source, **kwargs):
    return generate(source, kwargs.pop('start', START), kwargs.pop('end', AS_OF),
                    kwargs.pop('as_of', AS_OF), **kwargs)


def test_combined_daily_aggregation_and_readonly(source):
    before = extract(source)
    rows, _, report, metadata = build(source)
    assert extract(source) == before
    assert rows[0]['units_sold'] == 3
    assert rows[0]['sales_value'] == Decimal('.50')
    assert rows[0]['transaction_count'] == 2
    other = [r for r in rows if r['product_id'] == 2]
    assert other[0]['data_source'] == 'historical'
    assert other[0]['sales_value'] == Decimal('.30')
    assert other[1]['units_sold'] == 0
    assert other[2]['units_sold'] is None and other[2]['coverage_status'] == 'unknown'
    assert other[3]['observed_units_sold'] == 9 and other[3]['units_sold'] is None
    assert report['completed_days'] == 37
    assert report['confirmed_zero_sales_days'] == 1
    assert report['incomplete_days'] == 1
    assert metadata['dataset_kind'] == 'retrospective'
    assert max(r['date'] for r in rows) < AS_OF
    assert rows == sorted(rows, key=lambda r: (r['product_id'], r['date']))


def test_features_calendar_windows_and_product_isolation(source):
    _, features, _, _ = build(source)
    row = features[28]
    assert row['lag_1'] == 28 and row['lag_7'] == 22
    assert row['lag_14'] == 15 and row['lag_28'] == 3
    assert row['rolling_mean_7'] == 25
    assert row['rolling_std_7'] == pytest.approx(2.160246899469287)
    assert row['completed_days_28'] == 28
    assert features[0]['lag_1'] is None
    assert features[0]['rolling_mean_7'] is None
    other = [r for r in features if r['product_id'] == 2]
    assert other[3]['lag_1'] is None
    assert other[3]['completed_days_7'] == 2
    assert other[3]['rolling_mean_7'] is None
    assert row['day_of_week'] == row['date'].weekday()
    assert row['day_of_month'] == 29 and row['month'] == 1


def test_target_future_and_cross_product_leakage(source):
    rows = build(source)[0]
    original = engineer(rows)
    mutated = copy.deepcopy(rows)
    cutoff = START+timedelta(days=15)
    for row in mutated:
        if row['date'] >= cutoff or row['product_id'] == 2:
            row['units_sold'] = 100000
    changed = engineer(mutated)
    names = [name for name in original[0] if name.startswith(('lag_', 'rolling_', 'completed_days_'))]
    for a, b in zip(original, changed):
        if a['product_id'] == 1 and a['date'] <= cutoff:
            assert {n:a[n] for n in names} == {n:b[n] for n in names}


def test_warmup_history_and_readiness(source):
    _, full, _, _ = build(source)
    _, trimmed, report, _ = build(source, start=START+timedelta(days=28), min_complete_days=7)
    assert trimmed[0] == full[28]
    assert report['product_readiness'][0]['sufficient_observations']
    assert not report['product_readiness'][1]['sufficient_observations']
    assert 'not assessed' in report['product_readiness'][0]['ml_readiness']


@pytest.mark.parametrize('kwargs', [dict(start=AS_OF), dict(end=START-timedelta(days=1)),
    dict(as_of=date(2099,1,1)), dict(min_complete_days=0), dict(sku='MISSING'), dict(start='invalid')])
def test_invalid_parameters(source, kwargs):
    with pytest.raises(ValueError):
        build(source, **kwargs)


def test_observation_boundary_empty_and_selection(source):
    assert build(source, end=START-timedelta(days=1), start=START-timedelta(days=10))[0] == []
    selected = build(source, sku='SYN-2')
    assert all(r['product_id'] == 2 for r in selected[0])
    assert selected[2]['total_products'] == 1


@pytest.mark.parametrize('corruption', ['overlap', 'source', 'quantity', 'money', 'unknown', 'date', 'batch', 'coverage'])
def test_source_quality_errors(source, corruption):
    snapshot = extract(source)
    if corruption == 'overlap':
        snapshot['SalesRecord'][0]['product_id'] = 2
    elif corruption == 'source':
        snapshot['HistoricalSalesCoverage'][0]['source'] = 'wrong'
    elif corruption == 'quantity':
        snapshot['SalesRecord'][0]['quantity_sold'] = -1
    elif corruption == 'money':
        snapshot['SalesRecord'][0]['unit_selling_price'] = Decimal('NaN')
    elif corruption == 'unknown':
        snapshot['SalesRecord'][0]['product_id'] = 999
    elif corruption == 'date':
        snapshot['SalesRecord'][0]['sale_date'] = 'bad'
    elif corruption == 'batch':
        snapshot['HistoricalSale'][0]['import_batch_id'] = 999
    else:
        snapshot['SalesTrackingDay'].append(snapshot['SalesTrackingDay'][0])
    assert validate_sources(snapshot, AS_OF)[0]


def test_fail_closed_on_overlap(source):
    with Session(source) as db:
        db.add(SalesTrackingDay(product_id=2, sales_date=START))
        db.commit()
    with pytest.raises(DataQualityError):
        build(source)


def test_future_excluded_with_warning(source):
    with Session(source) as db:
        db.add(SalesRecord(product_id=1, sale_date=AS_OF, quantity_sold=9999, unit_selling_price=Decimal('1')))
        db.commit()
    result = build(source)
    assert any('excluded' in w for w in result[2]['validation_warnings'])
    assert max(r['date'] for r in result[0]) == AS_OF-timedelta(days=1)


def test_reproducible_exports_metadata_and_safe_paths(source, tmp_path):
    result = build(source)
    a = export_dataset(result, tmp_path/'a')
    b = export_dataset(build(source), tmp_path/'b')
    for name in ('daily_demand.csv', 'demand_features.csv', 'data_quality_report.json'):
        assert (a/name).read_bytes() == (b/name).read_bytes()
    ma = json.loads((a/'dataset_metadata.json').read_text())
    mb = json.loads((b/'dataset_metadata.json').read_text())
    assert ma.pop('generated_at') and mb.pop('generated_at')
    assert ma == mb and len(ma['dataset_checksum']) == 64
    assert ma['known_limitations'] and ma['schema_version'] == '1.0'
    with pytest.raises(FileExistsError):
        export_dataset(result, a)
    with pytest.raises(ValueError):
        export_dataset(result, tmp_path/'..'/'escape')
    text = (a/'daily_demand.csv').read_text()
    assert 'password' not in text and 'stock_quantity' not in text


def test_dataset_duplicate_and_missing_date_checks(source):
    rows, features, _, _ = build(source)
    products = extract(source)['Product']
    assert quality_report(rows+[rows[0]], features, products)['validation_errors']
    assert quality_report(rows[:2]+rows[3:], features, products)['validation_errors']


def test_empty_export(source, tmp_path):
    result = build(source, start=date(2025,1,1), end=date(2025,1,2))
    out = export_dataset(result, tmp_path/'empty')
    assert len((out/'daily_demand.csv').read_text().splitlines()) == 1
    assert result[2]['coverage_percentage'] == 0


def test_long_rolling_statistics(source):
    from statistics import mean, stdev
    rows, features, _, _ = build(source)
    for window in (7, 14, 28):
        values = [r['units_sold'] for r in rows[28-window:28]]
        assert features[28][f'rolling_mean_{window}'] == mean(values)
        assert features[28][f'rolling_std_{window}'] == stdev(values)


def test_retrospective_coverage_semantics(source):
    # Coverage timestamps may postdate the chosen as-of. Do not claim PIT safety.
    result = build(source, as_of=date(2026,1,4))
    assert result[0][0]['is_complete_day']
    assert result[3]['dataset_kind'] == 'retrospective'
    assert any('revision history' in text for text in result[3]['known_limitations'])


def test_export_formula_encoding(source, tmp_path):
    result = build(source)
    result[0][0]['sku'] = '=1+1'
    output = export_dataset(result, tmp_path/'formula')
    assert "'=1+1" in (output/'daily_demand.csv').read_text()


def test_no_source_database_creation(tmp_path):
    path = tmp_path/'missing.db'
    engine = create_engine(f'sqlite:///{path}')
    try:
        with pytest.raises(ValueError):
            extract(engine)
        assert not path.exists()
    finally:
        engine.dispose()


def test_export_rejects_directory_link(source, tmp_path):
    link = tmp_path/'linked'
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('Windows symlink privilege unavailable')
    with pytest.raises(ValueError):
        export_dataset(build(source), link/'export')
