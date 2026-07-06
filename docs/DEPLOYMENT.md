# Deployment — Personal AI Companion (`202.58.120.93`)

How the app is deployed to the public server, and the exact steps to update it.
Reconstructed from the live server + prior deploy session so future updates don't
have to rediscover it.

> TL;DR to ship a **UI change**: build locally, `rsync web/` to the server,
> restart `companion-api`, reload nginx. See [§4](#4-ui-only-deploy-fast-path).
> To ship **everything** (backend too): push `main`, then run
> [`deploy/update.sh`](#3-full-deploy-standard-path) on the server.

---

## 1. Server & access

| | |
|---|---|
| Host | `202.58.120.93` (public, IPv4) |
| Public URL | **`https://202-58-120-93.sslip.io`** (real Let's Encrypt cert) — see [§8](#8-https--tls-the-mic-needs-a-secure-context) |
| Ports | **443** (HTTPS, primary) · **80** (redirects → 443, also serves the ACME challenge) |
| SSH | `ssh -i /home/rabin/Documents/experiments/next-gen-nepse/trishul/cloud_access/id_ed25519_everestcloud-2026-06-15 root@202.58.120.93` |
| App dir | `/opt/companion` (a git checkout on branch `main`, owner `ubuntu`) |
| Env file | `/opt/companion/.env` (mode 600 — API keys; never printed/committed) |

> **Use the HTTPS URL.** The browser mic (`getUserMedia`, hence all voice) only
> works in a *secure context* — HTTPS or localhost. Plain `http://202.58.120.93`
> can render the UI but the mic never prompts. Give users
> `https://202-58-120-93.sslip.io`.

The SSH key lives outside this repo (in the `next-gen-nepse/trishul` cloud-access
folder). Keep it there; do not copy it into this project.

## 2. What runs on the box

```
Internet ─▶ nginx :443 (TLS) ──reverse proxy──▶ uvicorn 127.0.0.1:8000  (companion-api.service)
            nginx :80 ──301──▶ :443                  └─ serves web/dist (SPA + /assets + /pcm-worklet.js)
                                                      └─ /api/*  and  /ws/voice (wss)
           background worker ...................... companion-worker.service
           datastores (docker compose) ........... redis / qdrant / mongo / neo4j
```

- **nginx** (host package, `/etc/nginx/sites-enabled/companion`) terminates TLS on
  :443 and proxies **everything** to uvicorn — SPA, hashed assets, `/api/*`, and the
  `/ws/voice` WebSocket, which becomes `wss://` over TLS (the `Upgrade`/`Connection`
  headers are preserved, 3600s read timeout). :80 redirects to :443. It does **no
  response caching**, so "restarting nginx" never fixes a stale UI — the UI is
  whatever `web/dist` uvicorn is serving. The plain-:80 template lives in the repo at
  `deploy/nginx/companion.conf`; the live TLS server block + redirect are injected by
  `deploy/enable-https.sh` (certbot) — see [§8](#8-https--tls-the-mic-needs-a-secure-context).
- **systemd services** (units in `deploy/systemd/`):
  - `companion-api.service` → `uvicorn api.app:app` on `127.0.0.1:8000`
  - `companion-worker.service` → background consolidation/tools/search worker
- **Datastores** run via `docker compose` (`companion-redis|qdrant|mongo|neo4j`).

## 3. Full deploy (standard path)

Ships backend **and** frontend. Requires the work to be on `origin/main`.

```bash
# 0. from your workstation — publish the code first
git push origin main

# 1. on the server
ssh -i <key> root@202.58.120.93
sudo bash /opt/companion/deploy/update.sh
```

`deploy/update.sh` does: `git fetch/checkout/pull --ff-only origin main` →
`uv sync --extra voice` → `cd web && npm install && npm run build` → refresh systemd
units → `systemctl restart companion-api companion-worker` → `curl localhost:8000/health`.

> **Voice extra.** `update.sh` syncs with `--extra voice` (faster-whisper + pipecat
> +silero). Without it, `/ws/voice` returns
> `{"type":"error","message":"voice extra not installed"}` and voice is dead even
> over HTTPS. faster-whisper is CPU-viable (design doc), so it runs on this box —
> first model load is slow, then cached. See [§9](#9-voice-extra-wsvoice).

⚠️ **`update.sh` pulls from `origin/main`.** Anything not pushed there will NOT be
deployed — and worse, a `git pull` will **overwrite** files that were rsync'd but
never committed/pushed (see §4). Push first, or use the rsync fast-path knowingly.

## 4. UI-only deploy (fast path)

Use when you only changed `web/` and don't want to push/rebuild the backend (e.g.
backend work-in-progress isn't ready to publish). This is what was used to ship
the router / font / profile-panel / mobile-first UI.

```bash
cd /home/rabin/Documents/experiments/ai-friend

# 1. build locally (TypeScript strict + vite)
cd web && npm run build && cd ..

# 2. rsync the web tree to the server (build + source; node_modules excluded)
rsync -az --delete --exclude node_modules --exclude .vite \
  -e "ssh -i /home/rabin/Documents/experiments/next-gen-nepse/trishul/cloud_access/id_ed25519_everestcloud-2026-06-15" \
  ./web/ root@202.58.120.93:/opt/companion/web/

# 3. restart the app so it serves the fresh dist, and reload nginx
ssh -i <key> root@202.58.120.93 \
  'systemctl restart companion-api && nginx -t && systemctl reload nginx'
```

The app is a `HashRouter` SPA, so deep links look like
`http://202.58.120.93/#/login`. Vite emits **content-hashed** asset names, so a
new build changes `index-*.js`; a hard refresh (Ctrl/Cmd+Shift+R) clears any
cached `index.html` in the browser.

> Caveat: rsync'd-but-unpushed files are a landmine for the next `update.sh` —
> its `git pull` can revert them. The durable fix is to push the work to
> `origin/main` and deploy via §3.

## 5. Verify a deploy

```bash
# which bundle is public nginx serving?
curl -s http://202.58.120.93/ | grep -o 'assets/index-[^"]*\.js'   # should match web/dist/index.html
curl -s http://202.58.120.93/ | grep -o 'assets/index-[^"]*\.js'   # compare to local build
curl -s -o /dev/null -w '%{http_code}\n' http://202.58.120.93/     # 200
curl -s http://202.58.120.93/health                                 # {"status":"ok"} (proxied to uvicorn)
# on the box:
systemctl status companion-api --no-pager
journalctl -u companion-api -n 50 --no-pager
```

## 6. Known gap — `/api/me` (profile panel) not on the server yet

The UI's profile panel calls **`GET /api/me`**, but the deployed backend predates
that route: `api/routes/profile.py` is absent on the server and the live app only
exposes `/health, /api/chat, /, /pcm-worklet.js`. So on the public site the panel
loads its **error state** ("couldn't load profile"); the rest of the app (voice
session, orb, trace, auth page, theme, mobile layout) works.

To enable it, the backend must be deployed too — do a **full deploy (§3)** once
`api/routes/profile.py`, its `api/app.py` wiring, `api/deps.py` (`CurrentUser`),
and `ports/user_context.py` (`UserRecord`) are pushed to `origin/main`. A UI-only
rsync (§4) cannot add a backend route.

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "I don't see the new UI" | Server `web/dist` is stale, or browser cached `index.html`. Re-run §4; hard-refresh. nginx has no cache — restarting it won't help. |
| `/api/*` returns 404 | Route not in the deployed backend (see §6). Full deploy §3. |
| 502 Bad Gateway | uvicorn down: `journalctl -u companion-api -n 100`; `systemctl restart companion-api`. |
| WebSocket won't connect | nginx `/ws/voice` block / `Upgrade` headers; confirm `companion-api` is up. Over HTTPS the page must use `wss://` (the client derives this from `location.protocol`). |
| `update.sh` reverts a UI change | It was rsync'd but never pushed. Push to `origin/main`, redeploy §3. |
| Mic never prompts / no voice | Page is plain `http://` — not a secure context. Use `https://202-58-120-93.sslip.io` ([§8](#8-https--tls-the-mic-needs-a-secure-context)). |
| `/ws/voice` → `"voice extra not installed"` | The env was synced without `--extra voice`. Run `uv sync --extra voice` + restart ([§9](#9-voice-extra-wsvoice)); `update.sh` already does this. |
| Cert expired / renew | `certbot renew --dry-run`; the certbot systemd timer auto-renews (`systemctl list-timers | grep certbot`). |

---

## 8. HTTPS / TLS (the mic needs a secure context)

Browsers only expose `navigator.mediaDevices.getUserMedia` — the entire voice
feature — in a **secure context** (HTTPS or `localhost`). On plain `http://` the mic
never prompts. So HTTPS is mandatory, not cosmetic.

**No domain, but a real trusted cert anyway — via sslip.io.** sslip.io is free
wildcard DNS where `a-b-c-d.sslip.io` resolves to `a.b.c.d`. So
`202-58-120-93.sslip.io` → `202.58.120.93` with no DNS setup, and **Let's Encrypt
issues a real cert for that hostname** — no self-signed "Not secure / proceed anyway"
warning. Public URL: **`https://202-58-120-93.sslip.io`**.

Setup is one idempotent script (safe to re-run):

```bash
# on the server, after deploy/install-nginx.sh has installed the :80 proxy
sudo bash /opt/companion/deploy/enable-https.sh
```

`deploy/enable-https.sh` does: `ufw allow 443/tcp` (+80 for the ACME challenge) →
`apt-get install certbot python3-certbot-nginx` → set nginx `server_name` to the
sslip host → `certbot --nginx -d 202-58-120-93.sslip.io --redirect` (injects the
:443 server block + a :80→:443 redirect in place) → reload nginx. certbot installs a
**systemd renew timer**, so the cert auto-renews.

⚠️ Re-running `deploy/install-nginx.sh` rewrites the site file back to the plain-:80
template (dropping certbot's :443 block). If you do, just re-run
`deploy/enable-https.sh` — certbot re-detects the existing cert and re-wires TLS.

**Upgrading to your own domain later** (nicer URL): point an `A` record at
`202.58.120.93`, then `DOMAIN=yourdomain.com sudo bash deploy/enable-https.sh`
(it passes `-d $DOMAIN` to certbot and sets `server_name`). Same real-cert flow, no
code changes.

**Firewall note:** the host `ufw` allows 22/80/443. If :443 is ever unreachable
despite nginx listening, check for a **cloud-provider security group** in front of the
VM (separate from ufw) and open 443 there too.

**Verify HTTPS + `wss://`:**

```bash
# real cert — no -k needed
curl https://202-58-120-93.sslip.io/health          # {"status":"ok"}
curl -o /dev/null -w '%{http_code}\n' https://202-58-120-93.sslip.io/   # 200
curl -o /dev/null -w '%{http_code}\n' http://202-58-120-93.sslip.io/    # 301 -> https

# /ws/voice over TLS: expect a {"type":"ready",...} reply
python3 - <<'PY'
import asyncio, json, ssl, websockets
async def main():
    ctx = ssl.create_default_context()
    async with websockets.connect("wss://202-58-120-93.sslip.io/ws/voice", ssl=ctx) as ws:
        await ws.send(json.dumps({"token": "static_token_abc"}))
        print(await ws.recv())   # {"type":"ready",...}
asyncio.run(main())
PY
```

The web client already picks the scheme from the page:
`const proto = location.protocol === "https:" ? "wss" : "ws"` (`web/src/pages/CompanionPage.tsx`),
and `/api/*` are relative fetches — so an HTTPS page talks `wss://` with no mixed-content
block. No frontend change is needed for TLS.

## 9. Voice extra (`/ws/voice`)

The real-time voice session needs the **`voice` optional-dependency group** in
`pyproject.toml` (`pipecat-ai[silero]`) plus `faster-whisper` (a base dep). The VAD
adapter import (`adapters/vad/silero.py`) is what gates it: if the extra is missing,
`/ws/voice` authenticates but then replies
`{"type":"error","message":"voice extra not installed"}` and closes.

Install / keep it installed:

```bash
export PATH="/root/.local/bin:$PATH"
cd /opt/companion && uv sync --extra voice
systemctl restart companion-api
```

`deploy/update.sh` and `deploy/setup.sh` both use `uv sync --extra voice`, so every
deploy keeps voice available. faster-whisper is CPU-viable (design doc) — the **first**
conversation loads the STT model (slow, tens of seconds) and it's cached after.
