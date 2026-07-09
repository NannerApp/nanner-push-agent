"""Configuration for the Nanner push agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml


HOSTED_GATEWAY_URL = "https://gateway.nanner.app"
GATEWAY_VERIFY_TLS = True


@dataclass
class HTTPConfig:
    host: str = "0.0.0.0"
    port: int = 8421


@dataclass
class MQTTConfig:
    host: str
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    tls: bool = False
    reviews_topic: str = "frigate/reviews"


@dataclass
class FrigateConfig:
    base_url: str
    user: Optional[str] = None
    password: Optional[str] = None
    verify_tls: bool = False


@dataclass
class NotifyConfig:
    # Which review lifecycle events to act on. Per-device severity/camera/object filters live in the
    # device registry (set by the app), not here.
    types: list[str] = field(default_factory=lambda: ["new"])
    per_camera_cooldown: float = 20.0
    thumbnails: bool = True


@dataclass
class Config:
    state_file: str
    public_base_url: str
    http: HTTPConfig
    mqtt: MQTTConfig
    frigate: FrigateConfig
    notify: NotifyConfig

    @staticmethod
    def load(path: Optional[str] = None) -> "Config":
        path = path or os.environ.get("NANNER_AGENT_CONFIG", "config.yaml")
        with open(path, "r") as fh:
            raw = yaml.safe_load(fh) or {}

        mqtt_raw = raw.get("mqtt", {})
        frigate_raw = raw.get("frigate", {})
        if os.environ.get("NANNER_MQTT_PASSWORD"):
            mqtt_raw["password"] = os.environ["NANNER_MQTT_PASSWORD"]
        if os.environ.get("NANNER_FRIGATE_PASSWORD"):
            frigate_raw["password"] = os.environ["NANNER_FRIGATE_PASSWORD"]

        return Config(
            state_file=raw.get("state_file", "./identity.json"),
            public_base_url=(raw.get("public_base_url") or "").rstrip("/"),
            http=HTTPConfig(**raw.get("http", {})),
            mqtt=MQTTConfig(**mqtt_raw),
            frigate=FrigateConfig(**frigate_raw),
            notify=NotifyConfig(**raw.get("notify", {})),
        )
