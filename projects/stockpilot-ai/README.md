# StockPilot AI — Milestone 1

A small inventory management web application built with FastAPI, SQLAlchemy, SQLite, and Jinja2. The first milestone provides products, stock movement history, low-stock tracking, and a database-backed dashboard. There are no AI features in this milestone.

## Requirements

- Python 3.13
- PowerShell (commands below assume the repository is `C:\Users\User\ai-engineering` and its virtual environment is `.venv`)

## Install

From the repository root:

```powershell
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install -r .\projects\stockpilot-ai\requirements.txt
```

If PowerShell blocks activation, use the environment's interpreter directly in the commands below. `fastapi`, `SQLAlchemy`, `Jinja2`, and `uvicorn` were already installed in the inspected environment. `pytest`, `python-multipart`, and `httpx` may need installation from `requirements.txt`.

## Configure and initialize

The app defaults to `stockpilot.db` in the current working directory. Set `DATABASE_URL` in PowerShell to choose another SQLite location. `.env.example` documents the variable; this milestone reads environment variables directly and does not load `.env` files.

```powershell
Set-Location C:\Users\User\ai-engineering\projects\stockpilot-ai
$env:DATABASE_URL = "sqlite:///./stockpilot.db"
python -m app.init_db
```

The database is initialized explicitly and is never seeded on startup. To add example products and their opening inventory movements, run:

```powershell
python -m app.seed
```

The seed command is safe to repeat: it adds the sample catalog only when the database has no products.

## Run

```powershell
Set-Location C:\Users\User\ai-engineering\projects\stockpilot-ai
..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. If you activated the environment from the repository root, `python -m uvicorn app.main:app --reload` also works.

## Test

From the project folder:

```powershell
..\..\.venv\Scripts\python.exe -m pytest
```

The tests use a temporary SQLite database and cover product creation, duplicate SKU rejection, stock adjustment validation and history, low-stock calculations, and persistence across database sessions.

## Milestone 2A: Sales history

Sales are recorded separately from manual stock corrections and supplier receipts. Each sale stores the product, quantity, sale date, unit selling price snapshot, and creation timestamp. Recording a sale reduces stock and writes a sale-specific inventory movement in the same database transaction. If any part fails, all changes roll back.

After updating an existing installation, initialize the database from the project directory to create the new sales tables:

```powershell
Set-Location C:\Users\User\ai-engineering\projects\stockpilot-ai
..\..\.venv\Scripts\python.exe -m app.init_db
```

This uses SQLAlchemy `create_all`: it adds the new tables without changing existing product or movement rows. No sales demo data is generated. The existing `app.seed` command adds sample products only when the catalog is empty; it does not create sales records.

Open <http://127.0.0.1:8000/sales> to enter sales and review daily summaries. Each product/date must be explicitly marked fully tracked before its total is presented as observed demand. You can mark a verified no-sale date as complete, which records demand as zero. A date without this marker remains unknown; any sale quantity on an incomplete date is shown as a recorded partial total, not as complete demand. No forecasting model is included in this milestone.

Run the full test suite, including sales transaction, data coverage, persistence, and existing inventory tests, with:

```powershell
..\..\.venv\Scripts\python.exe -m pytest
```

## Troubleshooting

- **`No module named ...`**: activate the repository `.venv` and install `requirements.txt` as shown above.
- **`Form data requires "python-multipart"`**: install project requirements; this package is needed for HTML form handling.
- **Database location looks unexpected**: SQLite's relative URL is relative to the process working directory. Set `DATABASE_URL` to an absolute SQLite URL or start the app from the project folder.
- **PowerShell activation is blocked**: skip activation and invoke `..\..\.venv\Scripts\python.exe` directly.
- **Port 8000 is busy**: add `--port 8001` to the Uvicorn command.
