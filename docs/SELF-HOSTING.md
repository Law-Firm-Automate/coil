# Running Coil yourself

Coil is one container and one database file. It runs on a Mac mini in the office, a laptop, a
Windows PC with Docker Desktop, or a small cloud server. Your data never leaves the machine you
run it on.

## What you need

- A computer that stays on when you want to use Coil (a Mac mini or an old laptop is plenty).
- Docker Desktop (Mac, Windows) or Docker Engine (Linux). Free for a firm of your size.
- Ten minutes.

## Install on a Mac or Linux

Open Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/Senteras/coil/main/install.sh | sh
```

Then open http://localhost:8080 and create the owner account. That is the whole install.

## Install on Windows

1. Install Docker Desktop and start it.
2. Make a folder, for example `C:\coil`, and inside it save the two files below.
3. In PowerShell, run `cd C:\coil` then `docker compose up -d`.
4. Open http://localhost:8080.

`.env`:

```
SECRET_KEY=change-this-to-a-long-random-string
BASE_URL=http://localhost:8080
DATABASE_URL=sqlite:////app/data/practice.db
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
MAIL_FROM=
```

`docker-compose.yml`:

```yaml
services:
  coil:
    image: ghcr.io/senteras/coil:latest
    restart: unless-stopped
    env_file: .env
    ports:
      - "8080:8000"
    volumes:
      - ./data:/app/data
```

## Sending email

Invoices, engagement letters and portal links go out by email. Until you fill in the SMTP
settings, Coil stores them under Settings > Dev outbox so you can see what would have been sent.
Any mailbox works: Google Workspace (smtp.gmail.com, port 587, an app password), Microsoft 365
(smtp.office365.com, port 587), or a transactional provider. Set `MAIL_FROM` to the address
clients should reply to.

## Reaching it from outside the office

The install listens on your local network only. To use Coil from home or a phone:

- **Tailscale** (simplest): install it on the Coil machine and your devices, then use the
  Tailscale address. Nothing is exposed to the internet.
- **Cloudflare Tunnel**: gives you a real HTTPS address like coil.yourfirm.com without opening
  ports. Set `BASE_URL` to that address so links in emails work.
- **A reverse proxy** (Caddy, nginx) with HTTPS if you run it on a cloud server.

Do not forward port 8080 on your router to the internet. Coil expects HTTPS in front of it.

## Backups

Everything is in the `data` folder next to your `.env`: the database, uploaded documents,
generated PDFs. Copy that folder somewhere safe on a schedule (Time Machine, a cloud drive, or a
nightly `rsync`). To restore, put the folder back and start the container.

## Updating

```bash
cd ~/coil && docker compose pull && docker compose up -d
```

Your data folder is untouched by updates.

## Online payments

Create a Stripe account, paste the secret and publishable keys into `.env`, and point a Stripe
webhook at `BASE_URL/webhooks/stripe` for `checkout.session.completed` and
`checkout.session.async_payment_succeeded`. Restart the container after editing `.env`.

## Scheduled jobs

Coil sends the morning agenda, invoice and engagement reminders, and follow-up sequence steps
from a small command. Run these from cron or Task Scheduler on the Coil machine:

```
15 7 * * * cd ~/coil && docker compose exec -T coil python -m app.cli agenda
30 7 * * * cd ~/coil && docker compose exec -T coil python -m app.cli reminders
0  8 * * * cd ~/coil && docker compose exec -T coil python -m app.cli sequences
0  6 1 * * cd ~/coil && docker compose exec -T coil python -m app.cli interest
```

## Getting help

Open an issue at https://github.com/Senteras/coil/issues, or email the address on the Coil page
at lawfirmautomate.com/coil.
