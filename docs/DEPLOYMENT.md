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
| Host | `202.58.120.93` (public, IPv4) — served on port **80** |
| SSH | `ssh -i /home/rabin/Documents/experiments/next-gen-nepse/trishul/cloud_access/id_ed25519_everestcloud-2026-06-15 root@202.58.120.93` |
| App dir | `/opt/companion` (a git checkout on branch `main`, owner `ubuntu`) |
| Env file | `/opt/companion/.env` (mode 600 — API keys; never printed/committed) |

The SSH key lives outside this repo (in the `next-gen-nepse/trishul` cloud-access
folder). Keep it there; do not copy it into this project.

## 2. What runs on the box

```
Internet ─▶ nginx :80  ──reverse proxy──▶ uvicorn 127.0.0.1:8000  (companion-api.service)
                                              └─ serves web/dist (SPA + /assets + /pcm-worklet.js)
                                              └─ /api/*  and  /ws/voice
           background worker ...................... companion-worker.service
           datastores (docker compose) ........... redis / qdrant / mongo / neo4j
```

- **nginx** (host package, `/etc/nginx/sites-enabled/…`) proxies **everything** to
  uvicorn — SPA, hashed assets, `/api/*`, and the `/ws/voice` WebSocket (with
  `Upgrade`/`Connection` and a 3600s read timeout). It does **no response
  caching**, so "restarting nginx" never fixes a stale UI — the UI is whatever
  `web/dist` uvicorn is serving. The managed config also lives in the repo at
  `deploy/nginx/companion.conf`.
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
`uv sync` → `cd web && npm install && npm run build` → refresh systemd units →
`systemctl restart companion-api companion-worker` → `curl localhost:8000/health`.

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
| WebSocket won't connect | nginx `/ws/voice` block / `Upgrade` headers; confirm `companion-api` is up. |
| `update.sh` reverts a UI change | It was rsync'd but never pushed. Push to `origin/main`, redeploy §3. |
| voice extra / STT heavy on CPU host | Server runs base deps only (voice extra intentionally skipped); `STT_MODEL_SIZE` is small. |
