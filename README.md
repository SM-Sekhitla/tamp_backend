# TAMP backend

FastAPI + PostgreSQL backend for the TAMP React frontend. Its layout follows the PRISM reference repository: versioned API routes, controllers, services, schemas, database models, middleware, Alembic migrations, and tests.

## Docker startup

```bash
cd /home/hlaks/Documents/TAMP/tamp_backend
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Put a new `SECRET_KEY` and a separate `POSTGRES_PASSWORD` in `.env`. For local Mailjet delivery, set `MAILJET_API_KEY`, `MAILJET_SECRET_KEY`, and a verified `MAILJET_FROM_EMAIL`. Mailjet is used before the optional SMTP fallback. Alternatively, explicitly set `DEV_EMAIL_CODES=true` for local development. Production refuses development codes.

```bash
docker compose up --build -d
docker compose exec backend python -m scripts.create_admin admin@example.com
```

On first API startup with `BOOTSTRAP_SECRET` set, TAMP creates one platform `system_super_user` using `DEFAULT_PLATFORM_ADMIN_USERNAME` and `DEFAULT_PLATFORM_ADMIN_EMAIL`. Leave `DEFAULT_PLATFORM_ADMIN_PASSWORD` empty to generate a temporary password. The generated password is written to startup logs only when the account is created, so save it immediately. Later startups detect the existing account and do not log another password. Set a fixed bootstrap password only when explicitly required. The API listens at `http://localhost:5000`; API docs are at `/docs`, readiness at `/api/v1/health`, and liveness at `/healthz`.

The backend creates the shared `tamp-network` Docker network. Once it is healthy, start the sibling frontend using its own Compose file:

```bash
docker compose up --build -d --wait
cd /home/hlaks/Documents/TAMP/tamp_frontend/TAMP
docker compose up --build -d --wait
```

Open `http://localhost:3000`. nginx forwards `/api` to `http://backend:5000` over the shared network; PostgreSQL and the worker stay on the backend’s private network. If overriding `TAMP_NETWORK`, set the same value in both repositories. Stop the frontend before taking down the backend network. Verify the connection with `curl --fail http://localhost:3000/api/v1/health`.

For Vite development, set `VITE_API_TARGET=http://localhost:5000` in the frontend `.env` and run `npm run dev` from `tamp_frontend/TAMP`.

## Local development

Python 3.12+ and PostgreSQL 16+ are expected.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 5000 --reload --no-proxy-headers
```

No database tables or users are silently created when the API starts.

## Architecture and compatibility

The database uses separate domain tables with relational ownership and references. JSONB stores nested frontend documents such as locations, score breakdowns, and trip events while preserving the frontend's camelCase wire shape. Credentials, sessions, verification codes, and rate-limit counters use dedicated tables.

The frontend's existing `@trpc/client` requests are supported by `/api/trpc/{procedure}`. Equivalent operations are exposed under `/api/v1/{group}/{operation}` and documented in OpenAPI. This is a compatibility adapter for GET queries, POST mutations, batching, JSON envelopes, and errors; it is not a complete Python tRPC implementation.

Implemented areas include authentication, onboarding and verification, scoped snapshots, loads and trucks, matching and pricing, acceptance and trip commands, notifications, uploads, email codes, phone checks, maps, webhooks, and signed public tracking.

The backend owns prices, scores, receipts, trip transitions, and audit records. PostgreSQL transactions, locks, and uniqueness constraints prevent conflicting commercial commitments. The legacy snapshot endpoint validates each entity and ownership; it does not accept client-authored trips, receipts, or audit history.

## Security notes

- Passwords use Argon2. Opaque session cookies are HttpOnly and stored as keyed hashes.
- Password changes and resets revoke sessions. Email codes are hashed, expiring, one-use, and attempt-limited.
- Server-side role and record ownership checks apply to every private operation.
- Unsafe requests require `X-TAMP-Request: 1`; CORS origins are explicit and credentialed.
- Rate limits are PostgreSQL-backed. Bodies, batches, file sizes, and account storage are bounded.
- Only `TRUSTED_PROXY_IPS` may supply forwarded client addresses.
- Uploads are private; images are decoded and re-encoded. KYC PDFs download as attachments.
- Tracking links are signed, expire after seven days, and omit contacts, prices, receipts, and proof files.
- Webhooks require an explicit HTTPS hostname allowlist. The worker rejects private DNS targets, verifies TLS, avoids redirects, signs payloads, and retries. Delivery is at least once.

Mailjet or SMTP and `GOOGLE_MAPS_API_KEY` enable live email and Google Places/Routes. Without a Maps key, autocomplete uses a small South African city-centre directory and routes use the frontend's approximate fallback. Pricing comes from configurable `QUOTE_*` baseline settings; review those figures before commercial use. Google social login is not implemented.

For production, set `ENVIRONMENT=production`, secure cookies, unique credentials, explicit hosts/origins, Mailjet or SMTP, and TLS at the ingress. Add backups, monitoring, storage retention, and malware scanning appropriate to your environment.

## Validation

Tests require a disposable PostgreSQL database whose name includes `test`; its application tables are truncated.

```bash
export TEST_DATABASE_URL=postgresql+psycopg://tamp:password@localhost:5432/tamp_test
DATABASE_URL="$TEST_DATABASE_URL" alembic upgrade head
pytest -q
ruff check app scripts tests alembic
```

The suite covers registration, sessions, resets, CSRF, permissions, private uploads, rate limiting, signed tracking, generated matching, snapshot atomicity, concurrent acceptance, complete delivery, and webhook outbox handling. Playwright is not used.

## Render startup

For a single API instance, set Render's Docker Command to `sh /app/scripts/start_render.sh` after deploying an image containing this script. It runs migrations against `DATABASE_URL` before starting the API and uses Render's `PORT` value (default 5000). Do not wrap the command in quotes.

For multiple API instances, run `python -m alembic upgrade head` once in a pre-deploy job and use the normal API command for each instance.
