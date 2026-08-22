"""Nanner push agent — the privacy-holding core of notifications.

Runs on the user's LAN alongside Frigate. It owns the per-device notification preferences (they
never leave the network) and does all the work:
  * phones register their APNs token + filters directly with THIS agent (`/register`);
  * it subscribes to `frigate/reviews`, and for each review decides — per device — whether to
    notify, composes the notification, and forwards a ready-made payload to the publisher's gateway
    (`/push`), which is a dumb APNs relay;
  * it optionally serves a signed `/thumb` the app's extension fetches directly;
  * it prints a pairing code (its own address + secret) for the user to paste into the app.

Registration and thumbnails require the phone to reach the agent (LAN/Tailscale) — set
`public_base_url`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
import os
import random
import re
import ssl
import time
from typing import Any, Optional

import aiomqtt
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import GATEWAY_VERIFY_TLS, HOSTED_GATEWAY_URL, Config
from .frigate import FrigateClient
from .identity import Identity
from .registry import Device, Registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("agent")

# Frigate event IDs commonly contain a period (for example, a timestamp followed by a suffix).
# Slashes remain excluded so an ID cannot escape the thumbnail cache directory or URL segment.
_SAFE_EVENT_ID = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")
_MAX_CONCURRENT_PUSHES = 2
_MQTT_RECONNECT_MIN_SECONDS = 5.0
_MQTT_RECONNECT_MAX_SECONDS = 60.0


@dataclass(slots=True)
class Runtime:
    cfg: Config
    identity: Identity
    frigate: FrigateClient
    registry: Registry
    gateway: httpx.AsyncClient
    # Per (device_token, camera) throttle so review bursts do not spam a device or the gateway.
    last_push: dict[tuple[str, str], float] = field(default_factory=dict)
    mqtt_connected: bool = False
    mqtt_last_error: str | None = None
    mqtt_task: asyncio.Task[None] | None = None


def _log_pairing_code(runtime: Runtime) -> None:
    if not runtime.cfg.public_base_url:
        log.warning("public_base_url is not set, phones can't reach this agent to register.")
    code = runtime.identity.pairing_code(
        runtime.cfg.public_base_url or "http://SET-public_base_url"
    )
    log.info("=" * 72)
    log.info("Agent id: %s", runtime.identity.agent_id)
    log.info("PAIRING CODE (scan the QR below, or paste into Nanner ▸ Settings ▸ Notifications):")
    log.info("  %s", code)
    log.info("=" * 72)
    try:
        import qrcode

        qr = qrcode.QRCode(border=2)
        qr.add_data(code)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as exc:  # noqa: BLE001 - QR is optional; the text code still works
        log.info("(QR unavailable: %s)", exc)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = Config.load()
    identity = Identity.load_or_create(cfg.state_file)
    registry = Registry()
    try:
        frigate = FrigateClient(cfg.frigate)
        gateway = httpx.AsyncClient(
            verify=GATEWAY_VERIFY_TLS,
            timeout=httpx.Timeout(15.0, connect=10.0),
            limits=httpx.Limits(
                max_connections=_MAX_CONCURRENT_PUSHES,
                max_keepalive_connections=_MAX_CONCURRENT_PUSHES,
            ),
        )
    except Exception:
        registry.close()
        raise

    runtime = Runtime(cfg, identity, frigate, registry, gateway)
    app.state.runtime = runtime
    _log_pairing_code(runtime)
    runtime.mqtt_task = asyncio.create_task(mqtt_loop(runtime), name="mqtt-supervisor")
    cleanup_task = asyncio.create_task(_cache_cleanup_loop(runtime), name="thumbnail-cleanup")
    try:
        yield
    finally:
        tasks = [runtime.mqtt_task, cleanup_task]
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                log.error("background task %s failed during shutdown: %s", task.get_name(), result)
        await gateway.aclose()
        await frigate.close()
        registry.close()


app = FastAPI(title="Nanner Push Agent", lifespan=lifespan)


def _get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def _prune_cooldowns(runtime: Runtime, now: float) -> None:
    cutoff = now - runtime.cfg.notify.per_camera_cooldown
    expired = [key for key, pushed_at in runtime.last_push.items() if pushed_at < cutoff]
    for key in expired:
        del runtime.last_push[key]


def _allow(runtime: Runtime, device_token: str, camera: str, now: float) -> bool:
    key = (device_token, camera)
    cutoff = now - runtime.cfg.notify.per_camera_cooldown
    if runtime.last_push.get(key, 0.0) > cutoff:
        return False
    runtime.last_push[key] = now
    return True


# ---- phone-facing registration (secret-authenticated) -------------------------

def require_secret(request: Request, authorization: str = Header(default="")) -> None:
    runtime = _get_runtime(request)
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, runtime.identity.pairing_secret):
        raise HTTPException(status_code=401, detail="invalid pairing secret")


class RegisterRequest(BaseModel):
    device_token: str = Field(min_length=32, max_length=200)
    server_id: str = Field(min_length=1, max_length=100)
    environment: str = "production"
    severities: list[str] = Field(default_factory=lambda: ["alert"], max_length=10)
    cameras: Optional[list[str]] = Field(default=None, max_length=50)
    objects: Optional[list[str]] = Field(default=None, max_length=50)
    allow_unlabeled: bool = True


class UnregisterRequest(BaseModel):
    device_token: str = Field(min_length=1, max_length=200)


@app.post("/register", dependencies=[Depends(require_secret)])
async def register(req: RegisterRequest, runtime: Runtime = Depends(_get_runtime)):
    if req.environment not in ("sandbox", "production"):
        raise HTTPException(status_code=422, detail="bad environment")
    runtime.registry.upsert(
        device_token=req.device_token,
        environment=req.environment,
        server_id=req.server_id,
        severities=req.severities,
        cameras=req.cameras,
        objects=req.objects,
        allow_unlabeled=req.allow_unlabeled,
    )
    log.info("registered device %s…", req.device_token[:8])
    return {"ok": True}


@app.post("/unregister", dependencies=[Depends(require_secret)])
async def unregister(req: UnregisterRequest, runtime: Runtime = Depends(_get_runtime)):
    runtime.registry.remove(req.device_token)
    return {"ok": True}


# ---- thumbnails ---------------------------------------------------------------

def _thumb_signature(identity: Identity, event_id: str) -> str:
    return hmac.new(
        identity.pairing_secret.encode(), event_id.encode(), hashlib.sha256
    ).hexdigest()[:32]


def _thumbnail_url(runtime: Runtime, event_id: str) -> Optional[str]:
    if not (runtime.cfg.notify.thumbnails and runtime.cfg.public_base_url):
        return None
    signature = _thumb_signature(runtime.identity, event_id)
    return f"{runtime.cfg.public_base_url}/thumb/{event_id}.jpg?sig={signature}"


@app.get("/thumb/{event_id}.jpg")
async def thumb(event_id: str, sig: str = "", runtime: Runtime = Depends(_get_runtime)):
    if not _SAFE_EVENT_ID.match(event_id):
        raise HTTPException(status_code=400, detail="invalid event id")
    if not hmac.compare_digest(sig, _thumb_signature(runtime.identity, event_id)):
        raise HTTPException(status_code=403, detail="bad signature")
    path = await runtime.frigate.fetch_event_thumbnail(event_id)
    if not path:
        raise HTTPException(status_code=404, detail="no thumbnail")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/health")
async def health(runtime: Runtime = Depends(_get_runtime)):
    mqtt_task_running = runtime.mqtt_task is not None and not runtime.mqtt_task.done()
    return {
        "ok": True,
        "agent_id": runtime.identity.agent_id,
        "devices": runtime.registry.count(),
        "mqtt_connected": runtime.mqtt_connected,
        "mqtt_worker_running": mqtt_task_running,
        "mqtt_last_error": runtime.mqtt_last_error,
    }


@app.get("/ready")
async def ready(runtime: Runtime = Depends(_get_runtime)):
    if runtime.mqtt_task is None or runtime.mqtt_task.done():
        raise HTTPException(status_code=503, detail="MQTT worker is not running")
    if not runtime.mqtt_connected:
        raise HTTPException(
            status_code=503,
            detail=runtime.mqtt_last_error or "MQTT is not connected",
        )
    return {"ok": True}


# ---- review handling ----------------------------------------------------------

def _humanize(name: str) -> str:
    return name.replace("_", " ").title()


def _compose(server_id: str, camera: str, severity: str, review_id: str,
             objects: list[str], thumbnail_url: Optional[str]) -> dict:
    body = ", ".join(_humanize(o) for o in objects) or "Motion"
    payload = {
        "aps": {
            "alert": {"title": _humanize(camera), "body": f"{body} detected"},
            "sound": "default",
            "mutable-content": 1,
            "thread-id": camera,
            "interruption-level": "time-sensitive",
        },
        "server_id": server_id,
        "review_id": review_id,
        "camera": camera,
        "severity": severity,
        "objects": objects,
    }
    if thumbnail_url:
        payload["thumbnail_url"] = thumbnail_url
    return payload


async def handle_review(runtime: Runtime, after: dict[str, Any]) -> None:
    camera = after.get("camera", "")
    review_id = after.get("id", "")
    severity = after.get("severity", "")
    if not camera or not review_id:
        return

    data = after.get("data") or {}
    objects = data.get("objects") or []
    detections = data.get("detections") or []
    thumbnail_url = _thumbnail_url(runtime, detections[0]) if detections else None

    now = time.monotonic()
    _prune_cooldowns(runtime, now)
    batch: list[tuple[Device, dict[str, Any]]] = []
    for device in runtime.registry.all():
        if not device.wants(severity, camera, objects):
            continue
        if not _allow(runtime, device.device_token, camera, now):
            continue
        payload = _compose(device.server_id, camera, severity, review_id, objects, thumbnail_url)
        batch.append((device, payload))
        if len(batch) == _MAX_CONCURRENT_PUSHES:
            await asyncio.gather(
                *(push_to_gateway(runtime, device, payload) for device, payload in batch)
            )
            batch.clear()

    if batch:
        await asyncio.gather(
            *(push_to_gateway(runtime, device, payload) for device, payload in batch)
        )


async def push_to_gateway(runtime: Runtime, device: Device, payload: dict[str, Any]) -> None:
    body = {
        "agent_id": runtime.identity.agent_id,
        "gateway_credential": runtime.identity.gateway_credential,
        "device_token": device.device_token,
        "environment": device.environment,
        "payload": payload,
    }
    try:
        # Pushes are intentionally not retried. The per-camera cooldown and bounded batches keep a
        # degraded gateway from being hammered or producing duplicate notifications.
        resp = await runtime.gateway.post(f"{HOSTED_GATEWAY_URL}/push", json=body)
    except httpx.HTTPError as exc:
        log.warning("gateway push failed: %s", exc)
        return
    if resp.status_code != 200:
        log.warning("gateway /push returned %s: %s", resp.status_code, resp.text[:200])
        return
    try:
        result = resp.json()
    except ValueError:
        log.warning("gateway /push returned invalid JSON")
        return
    if not isinstance(result, dict):
        log.warning("gateway /push returned an unexpected response")
        return
    if result.get("dead"):
        log.info("pruning dead token %s… (%s)", device.device_token[:8], result.get("reason"))
        runtime.registry.remove(device.device_token)


async def mqtt_loop(runtime: Runtime) -> None:
    tls_context = ssl.create_default_context() if runtime.cfg.mqtt.tls else None
    reconnect_delay = _MQTT_RECONNECT_MIN_SECONDS
    while True:
        connected_at: float | None = None
        try:
            async with aiomqtt.Client(
                hostname=runtime.cfg.mqtt.host,
                port=runtime.cfg.mqtt.port,
                username=runtime.cfg.mqtt.username,
                password=runtime.cfg.mqtt.password,
                tls_context=tls_context,
            ) as mqtt:
                await mqtt.subscribe(runtime.cfg.mqtt.reviews_topic)
                connected_at = time.monotonic()
                runtime.mqtt_connected = True
                runtime.mqtt_last_error = None
                log.info(
                    "subscribed to %s on %s:%d",
                    runtime.cfg.mqtt.reviews_topic,
                    runtime.cfg.mqtt.host,
                    runtime.cfg.mqtt.port,
                )
                async for message in mqtt.messages:
                    try:
                        msg = json.loads(message.payload)
                    except (ValueError, TypeError):
                        log.warning("ignoring malformed MQTT review message")
                        continue
                    if not isinstance(msg, dict) or msg.get("type") not in runtime.cfg.notify.types:
                        continue
                    try:
                        await handle_review(runtime, msg.get("after") or {})
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - isolate one bad event from the MQTT worker
                        log.exception("failed to handle MQTT review; continuing")
                runtime.mqtt_last_error = "MQTT message stream ended"
                log.warning("MQTT message stream ended")
        except asyncio.CancelledError:
            raise
        except aiomqtt.MqttError as exc:
            runtime.mqtt_last_error = str(exc)
            log.warning("MQTT connection lost: %s", exc)
        except Exception as exc:  # noqa: BLE001 - supervisor must survive unexpected client errors
            runtime.mqtt_last_error = f"{type(exc).__name__}: {exc}"
            log.exception("unexpected MQTT worker error")
        finally:
            runtime.mqtt_connected = False

        # Reset only after a stable connection; rapidly flapping brokers receive exponential backoff.
        if connected_at is not None and time.monotonic() - connected_at >= 60.0:
            reconnect_delay = _MQTT_RECONNECT_MIN_SECONDS
        jittered_delay = reconnect_delay * random.uniform(0.8, 1.2)
        log.info("reconnecting to MQTT in %.1fs", jittered_delay)
        await asyncio.sleep(jittered_delay)
        reconnect_delay = min(reconnect_delay * 2, _MQTT_RECONNECT_MAX_SECONDS)


async def _cache_cleanup_loop(runtime: Runtime) -> None:
    """Remove cached thumbnails older than 7 days, checked hourly."""
    max_age = 7 * 24 * 3600
    while True:
        await asyncio.sleep(3600)
        try:
            now = time.time()
            for name in os.listdir(runtime.frigate._cache_dir):
                p = os.path.join(runtime.frigate._cache_dir, name)
                if os.path.isfile(p) and (now - os.path.getmtime(p)) > max_age:
                    os.remove(p)
                    log.debug("evicted cached thumbnail %s", name)
        except Exception as exc:
            log.warning("cache cleanup error: %s", exc)
