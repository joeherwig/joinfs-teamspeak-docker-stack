# joinfs-teamspeak-docker-stack

Docker Compose stack that combines a **JoinFS Hub**, a **TeamSpeak 3 server**, and a
**TS3 Query API** (Flask) that automatically moves pilots to TeamSpeak channels based
on their COM frequency when they connect to the JoinFS hub.

```
JoinFS client  →  JoinFS Hub (6112 UDP)
                       ↓ webhook PUT /usertochannel
               TS Query API (8081)
                       ↓ ServerQuery (10011)
               TeamSpeak 3 (9987 UDP)
```

## Overview

The stack is four containers plus a one-shot bootstrap job:

- **`joinfs`** — [JoinFS](https://joinfs.net) running in hub mode (`--hub --nogui --background`).
  This is the flight-sim networking session pilots connect to. It fires a webhook every time a
  connected aircraft's COM1/COM2 frequency changes (`--comswebhookuri http://tsapi:8081/usertochannel
  --comswebhookmethod PUT`), and also exposes a live WebSocket feed of aircraft data
  (`--websocket --websocketport 8765`) and a `whazzup.txt` feed (`--whazzup`) for external maps/status
  pages. `--hubname`/`--hubdomain` control how the hub appears in JoinFS's public hub list; they are
  unrelated to anything TeamSpeak-side (see [How channel switching works](#how-channel-switching-works)).
- **`tsapi`** (this repo's `tsquery_api/`) — a small Flask service that receives the JoinFS webhook,
  talks to TeamSpeak over the ServerQuery protocol (raw text protocol on TCP port 10011), and moves
  the matching pilot into the TeamSpeak channel for that frequency — creating it if needed.
- **`teamspeak`** — the official TeamSpeak 3 server image. Pilots connect to it with a normal
  TeamSpeak 3 client for voice.
- **`db`** — MariaDB, TeamSpeak's storage backend.
- **`ts3init`** — a one-shot job (runs once per fresh database, not on every restart) that logs in as
  `serveradmin` and grants the "Server Admin" server group the permissions `tsapi` needs (list
  clients/channels, move clients, create/edit channels).

## Services

| Container | Image | Purpose |
|---|---|---|
| `teamspeak` | `teamspeak` (official) | TeamSpeak 3 server with MariaDB |
| `db` | `mariadb` (official) | Database for TeamSpeak |
| `tsapi` | `${DOCKERHUB_USERNAME}/joinfs-tsapi` | Flask API — moves TS3 clients by COM frequency |
| `joinfs` | `${DOCKERHUB_USERNAME}/joinfs-console` | JoinFS Hub — fires a webhook on COM change |

`joinfs-console`'s source isn't part of this repo — it's built and published from wherever
its own source lives; this repo only consumes the prebuilt image.

## Connecting to TeamSpeak

Pilots/listeners just need a normal TeamSpeak 3 client pointed at this server's public IP or
hostname, port **9987** (default TeamSpeak port, no custom client config needed). There is no
plugin or special setup required on the TeamSpeak client side — channel assignment happens
entirely server-side via `tsapi` reacting to the JoinFS webhook (see below).

---

## Prerequisites

- Docker + Docker Compose plugin (`curl -fsSL https://get.docker.com | sh`)
- A server with a **public IP** and the firewall ports listed below open
- Docker Hub account (only needed if you want to publish your own images)

---

## Setup

### 1. Clone the repo

```bash
git clone git@github.com:joeherwig/joinfs-teamspeak-docker-stack.git
cd joinfs-teamspeak-docker-stack
```

### 2. Create `.env`

```bash
cp .env.example .env
# then edit .env and fill in DB_ROOT_PASSWORD, TS3_SERVERADMIN_PASSWORD, JOINFS_HUB_NAME,
# and JOINFS_HUB_DOMAIN (your server's public IP or hostname)
```

### 3. First boot — retrieve the TeamSpeak admin password

TeamSpeak generates a random `serveradmin` password on its very first start, unless you already
set `TS3_SERVERADMIN_PASSWORD` in `.env` before the first start (recommended — then you can skip
this step). Otherwise, capture the generated password:

```bash
docker compose up -d db teamspeak
sleep 15
docker compose logs teamspeak | grep -i 'password\|token\|serveradmin'
```

You will see something like:

```
password set to 'XXXXXXXXXXXXXXXX'
```

Edit `.env` and set `TS3_SERVERADMIN_PASSWORD` to this value.

> **This step is critical.** If the password is wrong, `tsapi` will log
> `TS3 login failed` and no channel moves will happen.

### 4. Start the full stack

```bash
docker compose up --build -d
```

### 5. Verify

```bash
docker compose ps
docker compose logs tsapi    # must NOT show "TS3 login failed"
docker compose logs joinfs   # look for "Opened UDP port 6112" and "Started hub"
```

Open JoinFS on your PC — your hub should appear in the global hub list.

---

## TeamSpeak ServerQuery — user and permissions

`tsapi` authenticates as `serveradmin` (the built-in TS3 admin account).
This gives it the permissions it needs:

- List clients and channels
- Create and edit channels
- Move clients between channels

**Common login failure causes:**

| Symptom in `docker compose logs tsapi` | Cause | Fix |
|---|---|---|
| `TS3 login failed: error id=520` | Wrong password in `.env` | Re-run step 3 and update `.env`, then `docker compose restart tsapi` |
| `TS3 login failed: error id=3329` | Too many failed logins — TS3 blocked the IP | Wait 5 min or restart `teamspeak`, then `docker compose restart tsapi` |
| Connection refused / timeout | TS3 not ready yet | `docker compose restart tsapi` after TS3 is fully up |

---

## How channel switching works

When a pilot changes COM1 in the sim, JoinFS fires:

```
PUT http://tsapi:8081/usertochannel
{
  "comsupdate": [
    { "callsign": "ZK-QMQ", "nickname": "FSC740", "com1": "124.85", "com2": "124.85" }
  ]
}
```

`tsapi` handles this in a **single ServerQuery session** (`process_single_com_update` in
`tsquery_api/app.py`):

1. **`clientlist`** — find the TS3 client for this pilot. A client matches if its nickname
   equals the callsign exactly, or starts with `<callsign>_`. If no matching client is
   connected to TeamSpeak, nothing else happens (no channel is created or touched).
2. **`channellist -topic`** — find the channel whose **topic** (not name, not description)
   equals the frequency, case-insensitively and trimmed. **Matching is topic-only** — the
   channel's name is cosmetic and never read for matching purposes.
3. **`channelcreate`** — if no channel's topic matched, create one: `channel_name` and
   `channel_topic` are both set to the frequency string, and it's created **permanent**
   (`channel_flag_permanent=1`). It's created permanent first because `clientmove` needs a
   channel that won't immediately vanish.
4. **`clientmove`** — move the pilot's client into the channel.
5. **`channeledit channel_flag_permanent=0`** — *only if the channel was just created in
   step 3 above, and the move succeeded* — flip it to non-permanent so TeamSpeak's own
   built-in cleanup deletes it once it's empty.

### The safety guarantee: manually created channels are never touched

Step 5 is gated by an in-memory "did I just create this channel in this request" flag —
there is no `channeldelete` call anywhere in this codebase. If step 2 finds an
**already-existing** channel via its topic — including one a TeamSpeak admin created by
hand through the TeamSpeak client — that flag is never set, so steps 3 and 5 are skipped
entirely. The app moves the pilot into it and otherwise leaves it completely alone: no flag
changes, no deletion, ever.

This means a TS admin can pre-provision named channels for specific frequencies:

1. Create a channel normally in the TeamSpeak client (any name, e.g. "London Approach").
2. Set its **Topic** field to the exact frequency string (e.g. `124.85`).
3. Leave it as a normal permanent channel (the default) — do **not** mark it temporary/semi-permanent.

`tsapi` will find and use that channel via the topic match and will never delete or modify
it. Only channels the app creates on the fly (because no existing channel's topic matched)
are ever flagged temporary and auto-removed by TeamSpeak when they empty out.

### "Hub name" vs. "callsign" — two unrelated concepts

- **`JOINFS_HUB_NAME`** is purely a JoinFS concept: the public display name for this hub,
  shown in everyone's JoinFS hub list. It has nothing to do with TeamSpeak channels or topics.
- **Callsign** is the pilot/aircraft identifier JoinFS sends in the webhook payload, used only
  to find which TS3 client to move (the nickname-matching rule in step 1 above).

---

## Updating a running server via SSH

If the server was deployed via `hetzner-cloud-config.yaml` (see below), updating to the
latest images is a single command:

```bash
ssh root@<server-ip>
systemctl restart joinfs.service   # pulls latest images, recreates containers
journalctl -u joinfs.service -f    # optional: watch pull/start progress
```

This is safe to run at any time — it does **not** delete the database, TeamSpeak
channels/permissions, or JoinFS state, since those live in named Docker volumes that
persist across restarts.

If you changed `compose.yaml`, `ts3init.py`, or `query_ip_allowlist.txt` themselves (not
just application code that gets rebuilt into an image), copy the updated files to
`/opt/joinfs/` on the server (e.g. via `scp`) before restarting — those files were seeded
once at first boot from the cloud-config and are not automatically kept in sync with this
repo.

For a manually-managed server (no systemd unit), the equivalent is:

```bash
cd joinfs-teamspeak-docker-stack
git pull
docker compose pull && docker compose up -d
```

---

## Publishing images to Docker Hub

This applies to `tsapi` only — `joinfs-console`'s source and publishing live outside this repo.

### Manual (run once from your dev machine)

```bash
docker login
docker build -t $DOCKERHUB_USERNAME/joinfs-tsapi:latest ./tsquery_api && docker push $DOCKERHUB_USERNAME/joinfs-tsapi:latest
```

### Automated with GitHub Actions

Every push to `main` automatically builds and publishes the `tsapi` image.
See [.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml).

**One-time setup — add secrets in your GitHub repo:**

Go to **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | your Docker Hub username |
| `DOCKERHUB_TOKEN` | Docker Hub → Account Settings → Security → **New Access Token** |

After any push to `main`, GitHub builds the image. The server then only needs
`docker compose pull && docker compose up -d` (or `systemctl restart joinfs.service` on
Hetzner) — no build step on the server itself.

---

## Hetzner deployment (fresh server)

The easiest path is to create a Hetzner server with the **Docker CE** app, and paste the
contents of [hetzner-cloud-config.yaml](hetzner-cloud-config.yaml) into the **Cloud-init user
data** field — after editing the `CHANGE_ME_*` placeholders described at the top of that file.
This automatically:

- Writes `/opt/joinfs/.env`, `compose.yaml`, `ts3init.py`, and `query_ip_allowlist.txt`
- Detects the server's public IP and fills it into `JOINFS_HUB_DOMAIN`
- Installs and starts a `joinfs.service` systemd unit that pulls and (re)starts the stack on
  every boot, and can be used to update the stack at any time via
  `systemctl restart joinfs.service` (see [Updating a running server via SSH](#updating-a-running-server-via-ssh))

For a manual (non-cloud-init) setup, it's the same as local Setup above:

```bash
curl -fsSL https://get.docker.com | sh
git clone git@github.com:joeherwig/joinfs-teamspeak-docker-stack.git
cd joinfs-teamspeak-docker-stack
# create .env as above, including the TS3 password from first-boot logs
docker compose up --build -d
```

### Required firewall ports

| Port | Protocol | Service |
|---|---|---|
| 22 | TCP | SSH |
| 6112 | UDP | JoinFS hub |
| 8765 | TCP | JoinFS WebSocket |
| 9987 | UDP | TeamSpeak voice |
| 10011 | TCP | TeamSpeak ServerQuery |
| 30033 | TCP | TeamSpeak file transfer |

---

## API endpoints (tsapi)

| Method | Path | Description |
|---|---|---|
| `PUT` | `/usertochannel` | Move pilot to COM frequency channel (called by JoinFS webhook) |
| `GET` | `/users` | List connected TS3 clients |
| `GET` | `/channels` | List TS3 channels |
| `PUT` | `/channel` | Create a channel manually |
| `PUT` | `/move` | Move a client by clid/cid |
