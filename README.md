# Close → Clay Sync

Syncs Close CRM Leads and Contacts to Clay workbooks via webhooks. Supports up to 50,000 records per workbook, runs an initial full sync on setup, then polls for changes every hour.

**Live app:** [clay.closekit.com](https://clay.closekit.com)

---

## How it works

1. User signs in with their Close account (OAuth)
2. User creates a sync — picks Lead or Contact, pastes a Clay webhook URL, optionally adds a Close filter
3. The app runs an initial full sync, pushing all matching records to Clay
4. APScheduler polls Close every hour for updated records and pushes changes to Clay
5. Clay deduplicates rows by `Close Id` so updates overwrite existing rows instead of creating new ones

---

## Local development

### Prerequisites

- Python 3.9+
- PostgreSQL running locally
- A Close OAuth app (see below)

### 1. Clone and install

```bash
git clone git@github.com:YOUR_USERNAME/clay-sync.git
cd clay-sync
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Create a local database

```bash
createdb closeclay
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
SECRET_KEY=any-random-string-for-local-dev
DATABASE_URL=postgresql://localhost/closeclay

CLOSE_CLIENT_ID=your_close_client_id
CLOSE_CLIENT_SECRET=your_close_client_secret
CLOSE_REDIRECT_URI=http://localhost:5001/auth/close/callback

SESSION_COOKIE_SECURE=false
```

### 4. Set up your Close OAuth app

1. Go to **app.close.com → Settings → OAuth Apps → New OAuth App**
2. Set the redirect URI to `http://localhost:5001/auth/close/callback`
3. Copy the Client ID and Secret into `.env`

### 5. Run database migrations

```bash
flask db upgrade
```

### 6. Start the app

```bash
python run.py
```

App runs at **http://localhost:5001**

---

## Production deployment (Fly.io)

The app is deployed to [Fly.io](https://fly.io) and served at `clay.closekit.com`.

### Prerequisites

- [Fly CLI](https://fly.io/docs/hands-on/install-flyctl/) installed and authenticated (`fly auth login`)
- A Close OAuth app with the production redirect URI added

### Initial setup (one-time)

**1. Create the app**
```bash
fly launch --name clay-closekit --region iad --no-deploy
```

**2. Create and attach a Postgres database**
```bash
fly postgres create --name clay-closekit-db --region iad
# Choose: Development (single node)
# Scale to zero: No

fly postgres attach clay-closekit-db -a clay-closekit
```

**3. Set secrets**
```bash
fly secrets set -a clay-closekit \
  SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  CLOSE_CLIENT_ID="your_close_client_id" \
  CLOSE_CLIENT_SECRET="your_close_client_secret" \
  CLOSE_REDIRECT_URI="https://clay.closekit.com/auth/close/callback" \
  SESSION_COOKIE_SECURE="true"
```

**4. Deploy**
```bash
fly deploy
```

The release command (`python migrate.py`) runs `flask db upgrade` automatically before each deployment.

**5. Add the custom domain**
```bash
fly certs add clay.closekit.com -a clay-closekit
fly certs show clay.closekit.com -a clay-closekit
```

Add a CNAME record in your DNS provider:

| Name | Type | Value |
|------|------|-------|
| `clay` | CNAME | `clay-closekit.fly.dev` |

**6. Update your Close OAuth app**

Add `https://clay.closekit.com/auth/close/callback` as an allowed redirect URI in **app.close.com → Settings → OAuth Apps**.

### Deploying updates

```bash
git add -A && git commit -m "your message"
fly deploy
```

### Viewing logs

```bash
fly logs -a clay-closekit
```

### Secrets reference

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | Set automatically by `fly postgres attach` |
| `SECRET_KEY` | Flask session signing key — long random hex string |
| `CLOSE_CLIENT_ID` | From your Close OAuth app |
| `CLOSE_CLIENT_SECRET` | From your Close OAuth app |
| `CLOSE_REDIRECT_URI` | `https://clay.closekit.com/auth/close/callback` |
| `SESSION_COOKIE_SECURE` | Set to `true` in production |

---

## Project structure

```
├── app/
│   ├── __init__.py        # App factory, scheduler init
│   ├── auth.py            # Close OAuth flow
│   ├── close_client.py    # Close API client (search, windowed fetch, records)
│   ├── clay_client.py     # Clay webhook client
│   ├── main.py            # Routes (dashboard, sync detail, API endpoints)
│   ├── models.py          # SQLAlchemy models (User, Sync)
│   ├── scheduler.py       # APScheduler setup, job registration
│   └── sync_engine.py     # Initial sync + poll logic
├── migrations/            # Flask-Migrate / Alembic migrations
├── static/
│   └── img/
│       └── close-mark.svg
├── templates/
│   ├── base.html
│   ├── dashboard.html
│   ├── index.html
│   └── syncs/
│       ├── detail.html
│       └── new.html
├── config.py              # App config (reads from environment)
├── Dockerfile
├── fly.toml
├── migrate.py             # Release command: runs db upgrade
├── requirements.txt
└── run.py                 # Dev server entry point
```
