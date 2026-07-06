#!/usr/bin/env bash
#
# Enable HTTPS (TLS on :443) for the Personal AI Companion.
# See docs/DEPLOYMENT.md for the full runbook.
#
# The box has no owned domain, but sslip.io provides free wildcard DNS:
# a-b-c-d.sslip.io resolves to a.b.c.d. So 202-58-120-93.sslip.io -> 202.58.120.93,
# and Let's Encrypt will issue a REAL trusted cert for it (no browser warning).
# A trusted HTTPS origin is what unlocks the mic: browsers only expose
# navigator.mediaDevices.getUserMedia in a secure context (HTTPS or localhost).
#
# Idempotent: safe to re-run. Opens the firewall, installs certbot, sets the
# nginx server_name to the sslip hostname, obtains/renews the cert via
# certbot --nginx (which injects the :443 server block + a :80->:443 redirect),
# and reloads nginx. certbot installs a systemd renew timer automatically.
#
# Prereqs: nginx already installed + the companion site enabled
# (deploy/install-nginx.sh), and the app listening on 127.0.0.1:8000.
#
# Usage:  sudo bash /opt/companion/deploy/enable-https.sh
# Override host/email:  DOMAIN=... EMAIL=... sudo bash deploy/enable-https.sh
#
set -euo pipefail

DOMAIN="${DOMAIN:-202-58-120-93.sslip.io}"
EMAIL="${EMAIL:-rabin.bhandari999@gmail.com}"
SITE="${SITE:-/etc/nginx/sites-enabled/companion}"
export DEBIAN_FRONTEND=noninteractive

echo "==> [1/5] open firewall for HTTPS (:443)"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 443/tcp || true
  ufw allow 80/tcp  || true   # ACME http-01 challenge + :80 redirect need :80
else
  echo "    ufw inactive/absent - skipping (ensure :80 and :443 are reachable)."
fi

echo "==> [2/5] install certbot + nginx plugin"
if ! command -v certbot >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y certbot python3-certbot-nginx
else
  echo "    certbot present: $(certbot --version 2>&1)"
fi

echo "==> [3/5] set nginx server_name to ${DOMAIN}"
if [ -f "$SITE" ]; then
  # certbot --nginx requires a matching server_name for -d. Idempotent replace.
  sed -i "s/server_name .*/server_name ${DOMAIN};/" "$SITE"
  nginx -t
  systemctl reload nginx
else
  echo "!! $SITE not found - run deploy/install-nginx.sh first." >&2
  exit 1
fi

echo "==> [4/5] obtain/renew Let's Encrypt cert + wire TLS via certbot --nginx"
certbot --nginx -d "$DOMAIN" --redirect \
  -m "$EMAIL" --agree-tos --no-eff-email --non-interactive --keep-until-expiring

echo "==> [5/5] validate + reload"
nginx -t
systemctl reload nginx

echo
echo "Done. HTTPS is live:  https://${DOMAIN}/"
echo "  - Real trusted cert (no browser warning); the mic (getUserMedia) now works."
echo "  - Auto-renew via the certbot systemd timer:  systemctl list-timers | grep certbot"
echo "  - Verify:  curl https://${DOMAIN}/health"
