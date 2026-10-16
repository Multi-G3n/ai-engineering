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

## Milestone 2B: Integrity and migrations

The schema is versioned with Alembic. Application startup does not migrate the database. Before upgrading an existing business database, stop the application and create a verified SQLite backup:

```powershell
Set-Location C:\Users\User\ai-engineering\projects\stockpilot-ai
$env:DATABASE_URL = "sqlite:///./stockpilot.db"
..\..\.venv\Scripts\python.exe -m app.backup_db
```

The command creates a timestamped, non-overwriting copy in `backups\` and checks its SQLite integrity. Confirm that the path is on a protected drive and that the backup is retained before proceeding. Then apply the migration explicitly:

The backup command prints both the resolved absolute source path and the generated backup path. It reserves each backup filename with an exclusive filesystem create, so a timestamp collision fails without opening the existing file. If the copy or its integrity check fails, cleanup is limited to the same filesystem object this invocation reserved.

```powershell
..\..\.venv\Scripts\python.exe -m app.init_db
```

`app.init_db` runs `alembic upgrade head`; alternatively use `..\..\.venv\Scripts\python.exe -m alembic upgrade head`. Set `DATABASE_URL` to the exact same database for the backup and migration commands. The migration preserves existing product and movement rows, creates missing sales tables, and adds nullable linkage and idempotency columns. It links an old sale to a movement only when the historical movement reason, product, quantity, and unique match identify one sale confidently. Ambiguous or unmatched historical rows remain unlinked.

The migration has no downgrade because dropping linkage or idempotency fields/tables can discard business data. To reverse an upgrade, stop the application and restore the verified pre-upgrade database backup as a whole. This also discards changes made after the backup. Do not restore over a database that has since received new business activity without first preserving that newer file.

Each displayed sale form includes a unique idempotency token. A retry with the same token is rejected with a clear message and cannot create another sale or stock deduction; a refreshed form receives a new token, so two genuine identical sales remain valid. A database uniqueness constraint protects the token across concurrent submissions. Sales, stock deduction, linked movement, and optional coverage marker commit in one transaction. SQLite foreign-key checking is enabled on every SQLAlchemy connection.

Demand summaries accept at most 3,660 inclusive dates (10 years). A tracked date with no sale is an observed zero; an untracked date remains unknown.

Install the added migration dependency and run tests from the project directory:

```powershell
..\..\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
..\..\.venv\Scripts\python.exe -m pytest
```

Migration tests build disposable SQLite databases and do not upgrade the application's configured database.

Relative SQLite URLs currently resolve from the process working directory for the application, backup, and Alembic commands. The backup utility reports the resolved source path so the operator can verify the target. A future project-root strategy could resolve relative SQLite paths against the project directory in shared configuration, with explicit absolute URLs continuing to take precedence. Adopting that strategy would change which database is selected when commands are launched outside the project directory; it should be a separately reviewed compatibility change with tests for both working-directory cases before rollout. For now, use an explicit absolute `DATABASE_URL` or launch commands from the project directory.

## Milestone 3B: Authentication and form security

Business pages require an administrator sign-in. Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes; there is no default account or password. The first administrator is created explicitly after the database has been backed up and upgraded through the normal migration process. From the project directory, configure the selected database and a long random session key, then run:

```powershell
Set-Location C:\Users\User\ai-engineering\projects\stockpilot-ai
$env:DATABASE_URL = "sqlite:///./stockpilot.db"
$env:STOCKPILOT_SECRET_KEY = (& ..\..\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))")
$env:STOCKPILOT_COOKIE_SECURE = "false" # Local HTTP development only
..\..\.venv\Scripts\python.exe -m app.backup_db
..\..\.venv\Scripts\python.exe -m app.init_db
..\..\.venv\Scripts\python.exe -m app.create_admin
..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

The backup and migration commands above are explicit; application startup does not migrate the database. Verify the printed backup path and target database before continuing. `app.create_admin` prompts for the username and password without echoing the password, confirms it, and stores only its hash. Do not put the generated session key or administrator password in source control. Keep the same secret key between restarts so active sessions remain valid; rotate it to invalidate all sessions. For deployment, provide `STOCKPILOT_SECRET_KEY` through the hosting environment or secret manager, terminate TLS, and leave `STOCKPILOT_COOKIE_SECURE=true` (the default). Setting it to `false` is only for local plain-HTTP development.

The session cookie is HTTP-only, SameSite=Lax, secure by default, and expires after eight hours. Login and logout as well as all product, stock, sales, and coverage forms require a session CSRF token. Login failures use the same message for unknown users and incorrect passwords. A process-local limiter allows five failed attempts per client address in a 15-minute window and temporarily blocks further attempts. It is not shared between multiple workers or machines; use a shared proxy/application rate limiter before running multiple workers. Account recovery, password reset, multi-factor authentication, and a web-based account-management page are not included.

Run all tests (they configure isolated temporary SQLite databases and test-only credentials):

```powershell
..\..\.venv\Scripts\python.exe -m pytest
```
