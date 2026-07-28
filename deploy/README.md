# Deploying JobPilot to an Oracle Cloud Always-Free VM

Target: an **Ampere A1 (ARM64) Ubuntu 22.04+** instance (always-free: up to 4 OCPU / 24 GB).
Two long-running services: the **scheduler** (the agent) and the **dashboard**.

## 1. Create the VM
- Oracle Cloud → Compute → Instances → Create. Shape **VM.Standard.A1.Flex** (ARM, always-free).
- Image: Ubuntu 22.04+. Add your SSH key.
- After boot, note the **public IP**. To reach the dashboard, add an **ingress rule** for TCP 8000 in the subnet's security list (and prefer putting a reverse proxy + auth in front — see step 7).

## 2. System packages
```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git nodejs npm \
  fonts-crosextra-carlito
fc-cache -f      # register Carlito so resume line-wraps match the locked layout
```
**Node.js** renders the resume kit (`Resume_creation/build.mjs`); **Carlito** is the
metric-identical Calibri clone the kit is measured for — without it the CV overflows
to a second page.

## 3. Get the code + Python deps
```bash
cd ~ && git clone <your-repo> hiring-automation   # or scp the folder up
cd hiring-automation
python3.12 -m venv .venv
. .venv/bin/activate
pip install -U pip -r requirements.txt
```

## 4. Chromium for the PDF step
```bash
python -m playwright install --with-deps chromium
```
`--with-deps` pulls the shared libs headless Chromium needs on Ubuntu. (Node +
Carlito were installed in step 2.) The resume pipeline is: tailored `resume.json`
→ `build.mjs` (Node) → HTML → this Chromium → one-page A4 PDF.

## 5. Secrets — create `.env` (never commit it)
```
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
DATABASE_URL=postgresql://...neon.tech/neondb?sslmode=require&channel_binding=require
GMAIL_APP_PASSWORD=...            # iamsomacharan@gmail.com app password
HUNTER_API_KEY=...                # optional (specific-recruiter discovery, later)
```

## 6. Initialise the database
```bash
python migrations/run.py          # idempotent; creates the 9 tables on Neon
```
Smoke-test the pipeline (still dry-run, sends nothing):
```bash
python -m jobpilot agent-run --no-jobspy
```

## 7. Install the services
Edit `User`/`WorkingDirectory`/paths in the two unit files if you didn't use
`ubuntu` + `~/hiring-automation`, then:
```bash
sudo cp deploy/jobpilot-scheduler.service deploy/jobpilot-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobpilot-scheduler jobpilot-dashboard
sudo systemctl status jobpilot-scheduler --no-pager
```
The scheduler now runs the full cycle daily at **08:00 IST**, follow-ups every 6h,
and reply-scans every 2h. Watch it: `journalctl -u jobpilot-scheduler -f`.

**Dashboard exposure:** binding to `0.0.0.0:8000` is open to the internet. Put
[Caddy](https://caddyserver.com) in front for HTTPS + basic-auth, or restrict the
Oracle ingress rule to your own IP. Don't leave it public and unauthenticated.

## 8. Going live (sending real emails)
Everything above runs in **dry-run** — it prepares drafts + tailored resumes and
logs them, but sends nothing. When you're ready:
1. Review the prepared emails + addresses on the dashboard **Applied** page.
2. In `config/settings.yaml`, set `outreach.dry_run: false`.
3. Leave `outreach.first_run_draft_only: true` for the first live day — the agent
   still drafts (doesn't send) so you can eyeball once more; then set it `false`.
4. `sudo systemctl restart jobpilot-scheduler`.

Guardrails stay active: **20 emails/day cap**, dedup (never re-email the same
job), and follow-ups stop the moment a company replies.
