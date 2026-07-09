# Nanner Push Agent

The **self-hosted** core of Nanner notifications. It owns your notification preferences (they never leave your network), watches Frigates MQTT events, decides what to notify,
composes each notification, and forwards ready-made payloads to the publisher's **push gateway**
(the `nanner-push-gateway` service), which is a dumb APNs relay. 

**You will need to have MQTT set up for your Frigate instance and an MQTT broker.**

```
Frigate ─▶ MQTT ─▶ agent ─▶ outbound /push ─▶ gateway ─▶ APNs─▶ device with Nanner
Nanner ─▶ register token & filters ─▶ agent
```

The gateway only relays, it never stores your thumbnails, camera names, detected objects, or which phone wants what.

## Reachability

Your phone registers its device and filters **directly with this agent**, and the extension fetches
thumbnails from it, so the agent must be reachable **by your phone**, on and off your LAN. Set
`public_base_url` to a stable address: a [Tailscale](https://tailscale.com) address works great. Plain-HTTP
or self-signed is fine, the app is built to accept them. Forwarding to the gateway is still outbound-only.

## Setup

```bash
cp config.example.yaml config.yaml     # set public_base_url, mqtt.host, frigate.base_url
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8421
```

Or Docker (see [`docker-compose.yml`](docker-compose.yml)):

```bash
mkdir -p config data && cp config.example.yaml config/config.yaml
docker compose up -d --build
```

On first start the agent prints a **pairing code** — as scannable text and a QR:

```
PAIRING CODE (scan the QR below, or paste into Nanner ▸ server ▸ Notifications):
  nanner-pair:eyJ1Ijoi...
  █▀▀▀▀▀█ ▀▄▀ █▀▀▀▀▀█
  █ ███ █ ▀█▀ █ ███ █   (scannable QR)
  ...
```
If using docker, you need to view the logs for your container e.g: `docker container logs nanner-agent`

In the Nanner app under the **Settings** tab, tap **Notifications** then **Scan QR Code** and point it
at the terminal, or paste the text code. The identity is saved to `state_file` (`identity.json`) so the code is stable across restarts back that file up if you don't want to re-pair.

The agent exposes two operational endpoints: `/health` is a liveness check and includes MQTT status,
while `/ready` returns `503` until the MQTT subscription is connected. Gateway pushes are limited to
two concurrent requests, are subject to the configured per-camera cooldown, and are not retried.

## What lives where

- **Filters** (severity/camera/object, plus "alerts with no object") — set in the app, stored here
  in `devices.db`, applied here. Each phone can have its own.
- **Composition** — the notification title/body is built here.
- **The gateway** — receives only `{agent_id, gateway_credential, device_token, environment, payload}` and
  relays it. No camera video or images ever pass through it.
