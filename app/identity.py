"""The agent's self-generated identity and the pairing code it prints for the app.

On first run the agent mints a random `agent_id` and `pairing_secret` and persists them, so the
pairing code stays stable across restarts. The pairing code is a base64url-encoded blob of the
agent URL + identity that the user pastes into Nanner to link their phone to this agent.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import tempfile


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def derive_gateway_credential(secret: str) -> str:
    """Domain-separated credential sent to the gateway. Never equals the pairing_secret."""
    return hmac.new(secret.encode(), b"nanner-gateway-v1", hashlib.sha256).hexdigest()


def derive_agent_id(gateway_credential: str) -> str:
    """Public identifier derived from the gateway credential (one-way from pairing_secret)."""
    return hashlib.sha256(gateway_credential.encode()).hexdigest()[:32]


class Identity:
    def __init__(self, pairing_secret: str):
        self.pairing_secret = pairing_secret
        self.gateway_credential = derive_gateway_credential(pairing_secret)
        self.agent_id = derive_agent_id(self.gateway_credential)

    @staticmethod
    def load_or_create(path: str) -> "Identity":
        directory = os.path.abspath(os.path.dirname(path) or ".")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        resolved_path = os.path.join(directory, os.path.basename(path))

        # Serializes first-run initialization across multiple processes. The lock file remains in
        # place so removing it can never race with another process waiting on the same inode.
        lock_fd = os.open(f"{resolved_path}.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if os.path.exists(resolved_path):
                os.chmod(resolved_path, 0o600)
                with open(resolved_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                return Identity(data["pairing_secret"])

            identity = Identity(secrets.token_urlsafe(32))
            temp_fd, temp_path = tempfile.mkstemp(prefix=".identity-", dir=directory)
            try:
                os.chmod(temp_path, 0o600)
                with os.fdopen(temp_fd, "w", encoding="utf-8") as fh:
                    json.dump({"pairing_secret": identity.pairing_secret}, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(temp_path, resolved_path)
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            return identity
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def pairing_code(self, agent_url: str) -> str:
        """Encodes the AGENT's address and secret. The app registers directly with the agent (which
        owns filters), so it needs the agent URL, not the gateway's."""
        blob = json.dumps(
            {"u": agent_url, "s": self.pairing_secret},
            separators=(",", ":"),
        )
        return "nanner-pair:" + _b64url(blob.encode())
