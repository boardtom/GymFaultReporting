# Fault Flag

QR-based equipment fault reporting. Scan a machine's code, submit a fault,
and it's emailed to the facilities mailbox and logged for tracking.

## Run it locally

```
python -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt

flask --app app seed            # loads the 8 sample machines in scripts/machines_seed.json
flask --app app run --debug
```

Open http://localhost:5000 to see the machine list, or go straight to
http://localhost:5000/report/RW-03 to try the report form. Admin pages
(the log, QR codes, machine list) are at `/log`, `/qr`, `/machines` and
need the admin password (default `changeme` — set your own in `.env`,
see `.env.example`).

With no `MAIL_SERVER` set, submitting a fault logs the email to the
console instead of sending it, so you can test the whole flow without
setting up SMTP first.

## Load your real machine list

The 8 machines in `scripts/machines_seed.json` are placeholders. Once you
have your real list of 200+, export it as a CSV with three columns —
`id,name,location` — and run:

```
flask --app app import-csv machines_full.csv
```

This adds new machines and updates existing ones (matched by ID), so you
can re-run it any time the list changes.

## Deploying to Render

1. **Push this folder to a GitHub repo** (Render deploys from Git, not a
   file upload). Create a repo, then from inside this folder:
   ```
   git init
   git add .
   git commit -m "Fault Flag"
   git branch -M main
   git remote add origin <your-repo-url>
   git push -u origin main
   ```

2. **Create the service on Render.**
   - Easiest: in the Render dashboard, choose **New > Blueprint**, point
     it at your repo, and it reads `render.yaml` to create both the web
     service and a free Postgres database automatically.
   - Or manually: **New > Web Service** from your repo, build command
     `pip install -r requirements.txt`, start command `gunicorn app:app`.
     Then **New > PostgreSQL** for the database, and copy its "Internal
     Database URL" into the web service's `DATABASE_URL` environment
     variable.

3. **Set the environment variables** (Render dashboard > your service >
   Environment). At minimum:
   - `ADMIN_PASSWORD` — a real password for `/log`, `/qr`, `/machines`
   - `APP_BASE_URL` — your Render URL once it's live, e.g.
     `https://fault-flag.onrender.com` (or your custom domain — see
     below). This is what gets baked into the QR codes and emails, so
     set it before generating and printing any codes.
   - `ALERT_TO_EMAIL` — the facilities mailbox
   - `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`,
     `MAIL_FROM_EMAIL` — your SMTP details. For Office 365:
     `smtp.office365.com`, port `587`, TLS on, and a mailbox
     username/password (or an app password if MFA is enforced — check
     with IT). For SendGrid: `smtp.sendgrid.net`, username `apikey`,
     password is your SendGrid API key.

   `SECRET_KEY` is generated automatically if you deployed via the
   Blueprint; otherwise set it to any random string.

4. **Seed or import your machine list** once the service is live. Render
   gives you a shell on the running service (dashboard > Shell tab):
   ```
   flask --app app seed
   ```
   or upload your CSV and run `flask --app app import-csv machines_full.csv`.

5. **Generate and print the QR codes** from `/qr` (sign in with your
   admin password first). Each card has a "Download PNG" link. Do this
   *after* `APP_BASE_URL` is set to its final value — the QR image
   encodes that URL directly, so codes generated before a domain change
   would need reprinting.

## Custom domain

Render supports custom domains on all plans. Point something like
`faultflag.wellnessinternational.co.uk` at the service (Render dashboard
> Settings > Custom Domains — it gives you a CNAME to add in your DNS),
then update `APP_BASE_URL` to match before printing QR codes.

## Notes on the free tier

- Render's free web service **spins down after 15 minutes of no
  traffic** and takes 30-60 seconds to wake on the next request — fine
  for a low-traffic internal tool, but the first scan after a quiet
  period will feel slow. Upgrade to a paid instance if that's a problem.
- Render's free Postgres database **expires after 90 days** unless
  upgraded to a paid plan (from about £6/month). Since this is where
  fault reports and status live, plan to upgrade before that point if
  you're relying on the log for real tracking.
