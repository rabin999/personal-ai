#!/usr/bin/env bash
#
# Install + wire the nginx reverse proxy that fronts the FastAPI serving edge.
# See docs/DEPLOYMENT.md for the full runbook.
#
# Idempotent: safe to re-run. Installs nginx, drops deploy/nginx/companion.conf
# into sites-available, enables it, removes the stock default site, validates the
# config, and (re)loads nginx. The app itself is expected to listen on
# 127.0.0.1:8000 (deploy/systemd/companion-api.service) so nginx is the only
# public entry point.
#
# NOTE: this installs the plain :80 template. To add TLS on :443 (real Let's
# Encrypt cert via sslip.io), run deploy/enable-https.sh AFTER this. Re-running
# install-nginx.sh overwrites the site config and drops certbot's :443 block, so
# re-run enable-https.sh afterwards to restore HTTPS.
#
# Usage:  sudo APP_DIR=/opt/companion bash deploy/install-nginx.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/companion}"
export DEBIAN_FRONTEND=noninteractive

echo "==> [1/4] install nginx"
if ! command -v nginx >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y nginx
else
  echo "    nginx present: $(nginx -v 2>&1)"
fi

echo "==> [2/4] install site config + rate-limit zone"
install -m 0644 "$APP_DIR/deploy/nginx/companion.conf" /etc/nginx/sites-available/companion
ln -sf /etc/nginx/sites-available/companion /etc/nginx/sites-enabled/companion
# Rate-limit zone (http context). companion.conf's `location /` references it, so it
# must be present before validation. See docs/DEPLOYMENT.md §11 (Security hardening).
install -m 0644 "$APP_DIR/deploy/nginx/companion-ratelimit.conf" /etc/nginx/conf.d/companion-ratelimit.conf
# The stock default site also binds :80 and would shadow us - remove it.
rm -f /etc/nginx/sites-enabled/default

echo "==> [3/4] validate config"
nginx -t

echo "==> [4/4] enable + reload"
systemctl enable nginx
systemctl reload nginx || systemctl restart nginx

echo
echo "Done. The app is now reachable on http://<server-ip>/"
echo "For HTTPS on :443, next run:  sudo bash deploy/enable-https.sh"
echo "Reload after config edits with:  sudo nginx -t && sudo systemctl reload nginx"
