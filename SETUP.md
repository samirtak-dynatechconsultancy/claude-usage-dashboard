# Hosted Dashboard Setup

End-to-end setup for the **server + collector** flavour of this project. The
original local-only tool (`python cli.py dashboard`) still works as-is and
needs none of this.

```
END-USER MACHINE              VERCEL                    SUPABASE
─────────────────             ──────                    ────────
collector.exe   ───HTTPS───▶  /api/ingest      ──────▶  Postgres (turns/sessions/users)
(scheduled 15m)               /api/upload-url  ──────▶  Storage  (raw JSONL)
                              /api/data        ◀──auth  Auth     (dashboard logins)
                              public/index.html
```

You'll do this **once**, in three phases:

1. Provision Supabase (database, storage, auth)
2. Deploy to Vercel (push to git, set env vars)
3. Build the installer & roll it out

---

## 1. Supabase project

### 1a. Create the project

1. <https://supabase.com/dashboard> → **New project**
2. Choose a region close to your team. Pick the **Free** tier to start (you
   can upgrade later without re-creating).
3. Save the database password — Supabase shows it once.

### 1b. Run the schema migrations

Open **SQL Editor** → **New query** and paste, in order:

1. [server/supabase/migrations/0001_initial_schema.sql](server/supabase/migrations/0001_initial_schema.sql)
2. [server/supabase/migrations/0002_recompute_function.sql](server/supabase/migrations/0002_recompute_function.sql)

Run each. Both are idempotent — safe to re-run.

> **Edit before running 0001:** find the line `INSERT INTO dashboard_users
> (email, role) VALUES ('samir.tak@dynatechconsultancy.com', 'admin')` and
> change the email to your own. That's the first account allowed to log into
> the dashboard.

### 1c. Configure Auth

**Authentication → Providers → Email** — leave "Enable Email Signup" ON.
Then **Authentication → URL Configuration**:

- **Site URL:** `https://YOUR-VERCEL-APP.vercel.app` (after step 2; come
  back and set it then if you don't have it yet)
- **Redirect URLs:** add `https://YOUR-VERCEL-APP.vercel.app/*`

### 1d. Grab your keys

**Project Settings → API**, copy these three:

| Variable | Where to use |
|---|---|
| `SUPABASE_URL` | Vercel env (both client + server) |
| `SUPABASE_ANON_KEY` | Vercel env. Public — embedded in browser JS. |
| `SUPABASE_SERVICE_ROLE_KEY` | Vercel env. **Server only.** Bypasses RLS. |

### 1e. Add more dashboard viewers (optional, anytime)

```sql
INSERT INTO dashboard_users (email, role) VALUES
  ('teammate1@company.com', 'viewer'),
  ('teammate2@company.com', 'viewer');
```

Anyone not in this table can't see the dashboard, even if they create a
Supabase account.

---

## 2. Vercel deploy

### 2a. Push the repo to git

```powershell
git init
git add .
git commit -m "Initial server + collector"
git branch -M main
git remote add origin https://github.com/YOUR-ORG/claude-usage.git
git push -u origin main
```

### 2b. Create the Vercel project

1. <https://vercel.com/new> → **Import** your repo.
2. **Framework Preset:** `Other`.
3. **Root Directory:** `server` (important — the [server/](server/) folder is
   the Vercel project root, not the repo root).
4. **Build & Output Settings:** leave defaults.

### 2c. Set environment variables

In **Project Settings → Environment Variables**, add for **Production**:

| Name | Value |
|---|---|
| `SUPABASE_URL` | From Supabase step 1d |
| `SUPABASE_ANON_KEY` | From Supabase step 1d |
| `SUPABASE_SERVICE_ROLE_KEY` | From Supabase step 1d |
| `INGEST_TOKEN` | Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `STORAGE_BUCKET` | `claude-raw` (matches the schema) |

Click **Deploy** (or `git push` — Vercel re-deploys on every commit to
`main`).

### 2d. Verify

After the first deploy completes:

- Open `https://YOUR-APP.vercel.app/login` — login screen should render.
- Send yourself a magic link to your admin email.
- After signing in you should land on the dashboard. It'll be empty until
  the first collector posts data.
- `https://YOUR-APP.vercel.app/api/config` should return JSON with your
  `SUPABASE_URL` + anon key.

Then go back to **Supabase → Auth → URL Configuration** and set Site URL
to your real Vercel URL (the placeholder from step 1c).

---

## 3. Build & distribute the installer

### 3a. Build on a Windows box

```powershell
cd installer
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" .\setup.iss
```

Output: `installer\Output\ClaudeUsageCollector-Setup-1.0.0.exe`.

(Install [Inno Setup 6](https://jrsoftware.org/isinfo.php) once, then this
is a 30-second command. The PyInstaller bundle weighs ~10 MB; the final
installer adds ~2 MB of overhead.)

### 3b. Send to teammates

**Interactive install:** double-click the .exe. The wizard prompts for
server URL and ingest token.

**Silent install (GPO / PDQ / Intune):**

```powershell
ClaudeUsageCollector-Setup-1.0.0.exe `
    /VERYSILENT /SUPPRESSMSGBOXES `
    /SERVERURL=https://YOUR-APP.vercel.app `
    /TOKEN=YOUR_INGEST_TOKEN
```

Within 15 minutes (or immediately, since the installer triggers a first
push), the dashboard will start filling in for that user.

---

## Verifying end-to-end

After step 3 on a test machine, you should see:

| Where | What to check |
|---|---|
| `%ProgramFiles%\ClaudeUsageCollector\` | `ClaudeUsageCollector.exe`, `config.json` |
| `taskschd.msc` → Task Scheduler Library | Task named `ClaudeCodeUsageCollector`, runs every 15 min |
| `%LOCALAPPDATA%\ClaudeUsageCollector\state.json` | Created after first push |
| `%LOCALAPPDATA%\ClaudeUsageCollector\collector.log` | Per-push log entries |
| Dashboard | User appears in the "User" filter dropdown; tokens accumulate |
| Supabase → Table Editor → `users` | One row per OS username |
| Supabase → Storage → `claude-raw` bucket | Raw JSONL files under `raw/<user>/<machine>/<hash>.jsonl` |

### Quick smoke test on the collector (without installing)

```powershell
cd collector
copy config.example.json config.json
notepad config.json     # paste your server URL + ingest token
python collector.py status
python collector.py push --dry-run
python collector.py push
```

---

## Troubleshooting

**Login page says "Failed to load /api/config"**  
You haven't set `SUPABASE_URL` / `SUPABASE_ANON_KEY` in Vercel, or the deploy
hasn't finished. Check **Vercel → Deployments** for build errors.

**Login works but dashboard shows "not authorized" / 403**  
Your email isn't in `dashboard_users`. Add it via the SQL editor (step 1e).

**Collector logs `HTTP 401 invalid ingest token`**  
The token in `config.json` doesn't match the `INGEST_TOKEN` env var on
Vercel. Regenerate one, set it in Vercel, redeploy, and reissue installers.

**Collector logs `HTTP 413` from /api/ingest**  
Vercel rejected the body for being too large. Reduce `INGEST_BATCH_SIZE`
in [collector/collector.py](collector/collector.py) (default 100; try 50).

**Dashboard charts are empty but the table has sessions**  
Range filter is excluding them. Click **All**.

**"View conversation" modal says `[file fetch error: ...]`**  
The raw JSONL didn't upload, or Storage RLS is blocking the server.
Verify the `claude-raw` bucket exists and that the
`SUPABASE_SERVICE_ROLE_KEY` env var is set on Vercel (not the anon key).

---

## Operating notes

- **Cost** at small-team scale (10 people, normal Claude Code usage): well
  inside Supabase Free and Vercel Hobby tiers. The constraining resource
  is Storage — each user's raw JSONL grows ~50-200 MB/month. Free tier is
  1 GB. Bump to Supabase Pro ($25/mo) when you outgrow that.
- **Adding users** to the dashboard: just `INSERT INTO dashboard_users`.
  No re-deploy needed.
- **Rotating the ingest token:** update `INGEST_TOKEN` on Vercel,
  redeploy, then rebuild + redistribute the installer. Old collectors
  immediately stop being able to post (return 401).
- **Removing a user's data:** `DELETE FROM users WHERE os_username = '…';`
  cascades to machines, sessions, and turns. Storage objects need manual
  removal via the Storage UI.
