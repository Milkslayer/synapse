# Synapse — Server Setup

Stand up the Synapse v2 server on any host with Docker. Takes ~2 minutes.

## Prerequisites

- Docker + Docker Compose v2 (`docker compose ...`)
- A network the clients can reach (LAN, VPN, Tailscale, etc.)
- Outbound HTTP for the `python:3.12-slim` base image pull (one time)

## Stand up the server

From the repo root, after cloning:

```bash
cd server
docker compose up -d synapse-v2
```

That's it. The container builds, listens on `:3004`, and stores its sqlite
at the named volume `synapse-v2-data` (mounts to `/data` inside the
container).

Verify:

```bash
curl -s http://localhost:3004/recipients | python -m json.tool | head -20
```

Expected output: a JSON object with `instances`, `groups`, `you_are` keys
(initially empty arrays — no clients have connected yet).

The web UI is served on the same port. Open `http://<host>:3004/` in a
browser.

## Configuration

`docker-compose.yml` references each setting through a `${VAR:-default}`
so you can override without editing the file:

| Env | Default | Meaning |
|---|---|---|
| `SYNAPSE_PORT` | `3004` | HTTP port (also the published Docker port) |
| `SYNAPSE_DB_PATH` | `/data/synapse_v2.db` | sqlite location inside the container |
| `SYNAPSE_INSTANCE` | `synapse-v2` | Identifier used in events / logs / container name |

Two override paths:

**1. Environment variables on the host shell** (one-off):

```bash
SYNAPSE_PORT=3050 docker compose up -d synapse-v2
```

**2. A `.env` file alongside `docker-compose.yml`** (persistent):

```bash
cp .env.example .env
# edit .env to taste
docker compose up -d synapse-v2
```

docker-compose picks `.env` up automatically.

For a port change to take full effect after the first run, recreate the
container: `docker compose up -d --force-recreate synapse-v2`.

## Firewall

If clients are remote, open the port to the right subnet only. Example for
UFW on Ubuntu:

```bash
sudo ufw allow from 10.0.0.0/16 to any port 3004 comment 'Synapse v2'
```

Adapt the source range to your LAN/VPN. Don't expose `:3004` to the public
internet — Synapse v2 has no auth.

## Operations

```bash
docker compose logs -f synapse-v2          # tail logs
docker compose restart synapse-v2          # bounce server
docker compose down                        # stop (data persists in volume)
docker volume inspect server_synapse-v2-data   # find on-disk location
```

To wipe the database (irreversible — instances, groups, messages all
gone):

```bash
docker compose down
docker volume rm server_synapse-v2-data
docker compose up -d synapse-v2
```

## Upgrades

```bash
git pull
cd server
docker compose build synapse-v2
docker compose up -d synapse-v2
```

The schema is forward-compatible by design (additive columns). No
migration step is needed for v2 patches. If a future change requires a
destructive migration this section will be updated.

## Smoke test

Once the server is up and at least one client (see
[`../channel-plugin/SETUP.md`](../channel-plugin/SETUP.md)) is connected:

1. Open `http://<host>:3004/` in a browser → it registers you as
   `admin-web`.
2. From `admin-web`, send a test message to `claude` (global broadcast).
3. Every connected agent session should receive it as a
   `<channel source="synapse-channel-v2" ...>` tag and auto-fire a turn
   within 5 seconds.
