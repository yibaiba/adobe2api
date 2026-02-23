import json
import base64
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
DATA_FILE = CONFIG_DIR / "tokens.json"
LEGACY_DATA_FILE = DATA_DIR / "tokens.json"

class TokenManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.tokens: List[Dict] = []
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self):
        with self._lock:
            source = DATA_FILE if DATA_FILE.exists() else LEGACY_DATA_FILE
            if source.exists():
                try:
                    self.tokens = json.loads(source.read_text(encoding="utf-8"))
                    if source == LEGACY_DATA_FILE and not DATA_FILE.exists():
                        DATA_FILE.write_text(json.dumps(self.tokens, indent=2), encoding="utf-8")
                except Exception:
                    self.tokens = []

    def save(self):
        DATA_FILE.write_text(json.dumps(self.tokens, indent=2), encoding="utf-8")

    def add(self, value: str):
        with self._lock:
            value = value.strip()
            if value.startswith("Bearer "):
                value = value[7:].strip()
                
            for t in self.tokens:
                if t["value"] == value:
                    return t
            
            new_token = {
                "id": uuid.uuid4().hex[:8],
                "value": value,
                "status": "active",
                "fails": 0,
                "added_at": time.time()
            }
            self.tokens.append(new_token)
            self.save()
            return new_token

    def remove(self, tid: str):
        with self._lock:
            self.tokens = [t for t in self.tokens if t["id"] != tid]
            self.save()

    def set_status(self, tid: str, status: str):
        with self._lock:
            for t in self.tokens:
                if t["id"] == tid:
                    t["status"] = status
                    t["fails"] = 0 if status == "active" else t["fails"]
            self.save()

    def get_available(self) -> Optional[str]:
        with self._lock:
            active = [t for t in self.tokens if t["status"] == "active"]
            if active:
                active.sort(key=lambda x: x["fails"])
                return active[0]["value"]

            # Auto-revive one recoverable token to avoid permanent 503
            # caused by transient upstream failures.
            recoverable = [t for t in self.tokens if t["status"] == "error"]
            if not recoverable:
                return None
            recoverable.sort(key=lambda x: x["fails"])
            chosen = recoverable[0]
            chosen["status"] = "active"
            chosen["fails"] = max(0, int(chosen.get("fails", 0)) - 1)
            self.save()
            return chosen["value"]

    def report_exhausted(self, value: str):
        with self._lock:
            for t in self.tokens:
                if t["value"] == value:
                    t["status"] = "exhausted"
            self.save()

    def report_error(self, value: str):
        with self._lock:
            for t in self.tokens:
                if t["value"] == value:
                    t["fails"] += 1
                    if t["fails"] >= 8:
                        t["status"] = "error"
            self.save()

    def report_success(self, value: str):
        with self._lock:
            for t in self.tokens:
                if t["value"] == value:
                    t["fails"] = max(0, int(t.get("fails", 0)) - 1)
                    if t["status"] == "error":
                        t["status"] = "active"
            self.save()

    @staticmethod
    def _decode_jwt_exp(value: str) -> Optional[int]:
        token = str(value or "").strip()
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        try:
            raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
            data = json.loads(raw.decode("utf-8", errors="ignore"))
            exp = data.get("exp")
            if isinstance(exp, (int, float)):
                return int(exp)
        except Exception:
            return None
        return None

    def list_all(self):
        with self._lock:
            res = []
            now_ts = int(time.time())
            for t in self.tokens:
                # mask value
                val = t["value"]
                masked = val[:15] + "..." + val[-10:] if len(val) > 30 else "***"
                exp_ts = self._decode_jwt_exp(val)
                remaining_seconds = None
                exp_readable = None
                if exp_ts is not None:
                    remaining_seconds = exp_ts - now_ts
                    try:
                        exp_readable = datetime.fromtimestamp(exp_ts).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        exp_readable = str(exp_ts)
                res.append({
                    "id": t["id"],
                    "value": masked,
                    "status": t["status"],
                    "fails": t["fails"],
                    "added_at": t["added_at"],
                    "expires_at": exp_ts,
                    "expires_at_text": exp_readable,
                    "remaining_seconds": remaining_seconds,
                    "is_expired": bool(exp_ts is not None and remaining_seconds is not None and remaining_seconds <= 0),
                })
            return res

token_manager = TokenManager()
