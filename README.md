# AI Avatar Studio

AI Avatar Studio is a full-stack, asynchronous image-generation product for creating avatars and visual concepts from text, uploaded images, or optional reference-image analysis.

The repository is organized as a single production-shaped application: a Next.js client, a FastAPI service, PostgreSQL business storage, Redis/Celery background jobs, and an S3-compatible storage adapter. The public API is frozen in OpenAPI and is the boundary between the Python and TypeScript codebases.

## Why this project is interesting

- **Complete generation loop:** invite access, email verification, image upload, prompt composition, asynchronous generation, result history, downloads, sharing, and feedback.
- **Correct money-like accounting:** point reservations, consumption, release, idempotency, and audit history are modeled as durable database facts instead of UI state.
- **Recoverable background work:** Celery Worker and Beat handle generation, recovery scans, heartbeat checks, and lifecycle cleanup without moving business rules into task wrappers.
- **Privacy-aware sharing:** only explicitly selected successful result assets can be shared; anonymous reads validate share state, expiry, and asset validity.
- **Clean service boundaries:** business rules live in services, persistence lives in repositories, and storage/AI providers are accessed through ports and adapters.
- **Realistic verification:** unit, API, isolated-database, migration, concurrency, frontend, and browser E2E coverage are kept in the repository.

## Product capabilities

| Capability | Current behavior |
| --- | --- |
| Text creation | Generate two images from a user-editable description. |
| Image transformation | Upload one source image and generate two transformed results. |
| Reference-image inspiration | Optional, server-gated image analysis that returns an editable description; it does not claim to recover an original prompt. |
| Results | Track task state, show two outputs, provide short-lived download URLs, and recover after refresh. |
| Sharing | Create an unpredictable public link for one or two explicitly selected result assets, with expiry and revocation. |
| Feedback | One durable feedback record per user and generation, with request idempotency. |
| Operations | Mock provider by default, PostgreSQL migrations, Celery recovery, object-storage cleanup, and append-only audit history. |

## Architecture

```text
Browser
  -> Next.js frontend (same-origin API proxy)
  -> FastAPI /api/v1
     -> Services -> Repositories -> PostgreSQL
                  -> StorageAdapter -> MinIO / S3-compatible storage
                  -> AIProvider -> Mock or configured provider
     -> Redis broker -> Celery Worker
                       Celery Beat only schedules registered tasks
```

The main boundaries are:

- `frontend/` — Next.js, React, TypeScript UI and generated API client.
- `backend/` — FastAPI routes, services, repositories, models, migrations, providers, storage, Celery tasks, and tests.
- `shared/api-contract/` — checked-in OpenAPI baseline consumed by both runtime tests and the frontend client.
- `docker/` — local seven-service Compose topology and environment template.
- `docs/` — architecture, product direction, task cards, handoff records, and engineering decisions.
- `scripts/` — local stack and verification helpers; no business rules.

Read the [architecture overview](docs/architecture/01-overview.md), [repository boundary](docs/architecture/09-repository-boundary.md), and [API contract](docs/architecture/03-api-contract.md) before changing a cross-layer behavior.

## Local development

### Prerequisites

- Python 3.13 or a compatible Python 3 release
- Node.js 24 and npm
- Docker Desktop with Compose v2

### Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

The backend expects PostgreSQL, Redis, and an S3-compatible storage service. For the isolated local stack, use the Compose instructions below. `AI_PROVIDER=mock` is the safe default for development and tests.

### Frontend

```powershell
cd frontend
npm ci
npm run dev
```

The browser uses the same-origin `/api/v1` path. When running Next.js directly, set `NEXT_PUBLIC_API_BASE_URL` and `API_PROXY_TARGET` for the environment you are using; these values are build-time configuration.

### Docker Compose

```powershell
Copy-Item docker/.env.example .env.v1
docker compose --env-file .env.v1 -f docker/compose.yml config
docker compose --env-file .env.v1 -f docker/compose.yml up --build -d
docker compose --env-file .env.v1 -f docker/compose.yml ps
```

The Compose file defines `frontend`, `backend`, `worker`, `beat`, `postgres`, `redis`, and `minio`. PostgreSQL, Redis, and MinIO bind to loopback by default. Stop the stack with:

```powershell
docker compose --env-file .env.v1 -f docker/compose.yml down
```

Do not use `down -v` unless you intentionally want to delete local development data.

## Verification

The latest isolated validation recorded in the handoff materials includes:

| Area | Result |
| --- | --- |
| Backend non-database tests | 1,502 passed |
| Isolated database and migration tests | 140 passed |
| OpenAPI/error-contract checks | 66 passed |
| Frontend Vitest | 181 passed across 21 files |
| TypeScript and Next.js production build | Passed |
| Browser E2E, native stack | 17 passed / 0 failed |
| Browser E2E, Compose stack | 17 passed / 0 failed |

Run focused checks from the repository root as follows:

```powershell
cd backend
pytest tests --ignore=tests/db --ignore=tests/e2e -q
pytest tests/api/test_openapi_contract.py tests/api/test_error_envelope.py -q
python -m compileall app tests alembic

cd ..\frontend
npm test -- --no-file-parallelism
npm run typecheck
npm run build
```

Database tests and browser E2E require an isolated environment. They must not be pointed at a shared database or a production storage bucket.

## API and data safety

- The checked-in OpenAPI document is the contract baseline: [`shared/api-contract/openapi.json`](shared/api-contract/openapi.json).
- Secrets belong only in ignored environment files. Never commit `.env.v1`, `.env.eng10`, provider keys, database passwords, signed URLs, or real user images.
- The default provider is Mock. Enabling a real provider is an explicit server-side deployment decision.
- Alembic migrations are append-only. Test migrations against an isolated database before applying them anywhere shared.

## Repository guide

- [Product direction](docs/product/00-product-direction.md)
- [Database design](docs/architecture/02-database-design.md)
- [AI Provider and storage architecture](docs/architecture/04-ai-provider.md)
- [System architecture and deployment](docs/architecture/05-system-architecture.md)
- [Engineering task index](docs/task-cards/00-index.md)
- [Local regression task](docs/task-cards/ENG-10-local-regression.md)

## Project status

This repository is a portfolio-quality engineering implementation and local validation environment. The codebase has completed the ENG-09 sharing/feedback/lifecycle scope and the ENG-10 local regression scope. Production deployment still requires environment-specific secrets, domain/TLS setup, persistent infrastructure, backups, monitoring, and a final release review.
