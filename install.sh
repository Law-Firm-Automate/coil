#!/usr/bin/env sh
# Coil installer for Mac and Linux. Needs Docker (Docker Desktop on a Mac).
#   curl -fsSL https://raw.githubusercontent.com/Senteras/coil/main/install.sh | sh
set -e
DIR="${COIL_DIR:-$HOME/coil}"
PORT="${COIL_PORT:-8080}"
IMAGE="${COIL_IMAGE:-ghcr.io/senteras/coil:latest}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. On a Mac, install Docker Desktop from https://www.docker.com/products/docker-desktop/ and run this again."
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but not running. Start Docker Desktop and run this again."
  exit 1
fi

mkdir -p "$DIR/data"
cd "$DIR"

if [ ! -f .env ]; then
  SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  cat > .env <<ENV
SECRET_KEY=$SECRET
BASE_URL=http://localhost:$PORT
DATABASE_URL=sqlite:////app/data/practice.db
# Email: fill these in to send invoices and letters. Leave blank and Coil keeps them in Settings > Dev outbox.
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
MAIL_FROM=
# Online payments (optional): from your Stripe dashboard.
STRIPE_SECRET_KEY=
STRIPE_PUBLISHABLE_KEY=
STRIPE_WEBHOOK_SECRET=
ENV
  chmod 600 .env
  echo "Wrote $DIR/.env (edit it to add email and Stripe later)."
fi

cat > docker-compose.yml <<YML
services:
  coil:
    image: $IMAGE
    container_name: coil
    restart: unless-stopped
    env_file: .env
    ports:
      - "$PORT:8000"
    volumes:
      - ./data:/app/data
YML

if ! docker compose pull 2>/dev/null; then
  echo "Could not pull the prebuilt image; building from source instead (takes a few minutes)."
  if ! command -v git >/dev/null 2>&1; then echo "git is needed to build from source."; exit 1; fi
  [ -d src ] || git clone --depth 1 https://github.com/Senteras/coil.git src
  (cd src && git pull -q || true)
  sed -i.bak "s#image: .*#build: ./src#" docker-compose.yml && rm -f docker-compose.yml.bak
  docker compose build
fi
docker compose up -d
echo
echo "Coil is running."
echo "  Open:    http://localhost:$PORT"
echo "  Files:   $DIR   (back up the data folder; it is your whole practice)"
echo "  Update:  cd $DIR && docker compose pull && docker compose up -d"
echo "  Stop:    cd $DIR && docker compose down"
echo
echo "First visit creates the owner account. Do that now, before anyone else can reach the port."
