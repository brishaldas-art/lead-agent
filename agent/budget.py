import os
import json
import time
from pathlib import Path
import collections
from datetime import datetime
from zoneinfo import ZoneInfo

class Budget:
    """Tracks API usage quotas and token accounting."""

    _state_path = Path("outputs/.budget.json")

    def __init__(self, rpm: int, tpm: int, rpd: int, max_calls: int, safety: float = 0.8):
        self.rpm = rpm
        self.tpm = tpm
        self.rpd = rpd
        self.max_calls = max_calls
        self.safety = safety
        self.run_calls = 0
        self.cache_hits = 0
        self.tokens: dict[str, list[int]] = {}
        self._windows: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._load_state()

    @classmethod
    def from_env(cls):
        rpm = int(os.getenv("RPM_LIMIT", "5"))
        tpm = int(os.getenv("TPM_LIMIT", "250000"))
        rpd = int(os.getenv("RPD_LIMIT", "20"))
        max_calls = int(os.getenv("MAX_CALLS_PER_RUN", "15"))
        safety = float(os.getenv("SAFETY", "0.8"))
        return cls(rpm, tpm, rpd, max_calls, safety)

    def allow(self, model: str, est_tokens: int) -> bool:
        if self.run_calls >= self.max_calls:
            return False
        if model in self.state.get("dead", []):
            return False
        if self.state["used"].get(model, 0) >= self.rpd:
            return False
        return True

    def wait_for_slot(self, model: str, est_tokens: int):
        dq = self._windows[model]
        max_rpm = self.rpm * self.safety
        max_tpm = self.tpm * self.safety
        while True:
            now = time.time()
            # Remove entries older than 60 seconds
            while dq and now - dq[0][0] > 60:
                dq.popleft()
            recent_calls = len(dq)
            recent_tokens = sum(t for _, t in dq)
            if recent_calls < max_rpm and (recent_tokens + est_tokens) <= max_tpm:
                break
            time.sleep(0.5)
        # Slot reserved – actual recording is done by ``record``
        dq.append((now, est_tokens))

    def record(self, model: str, tag: str, usage):
        self.run_calls += 1
        self.state["used"][model] = self.state["used"].get(model, 0) + 1
        prompt = getattr(usage, "prompt_token_count", 0) or 0
        output = getattr(usage, "candidates_token_count", 0) or 0
        thought = getattr(usage, "thoughts_token_count", 0) or 0
        totals = self.tokens.setdefault(tag, [0, 0, 0])
        totals[0] += int(prompt)
        totals[1] += int(output)
        totals[2] += int(thought)
        self._save_state()

    def mark_exhausted(self, model: str):
        dead = self.state.setdefault("dead", [])
        if model not in dead:
            dead.append(model)
            self._save_state()

    def summary(self) -> str:
        lines = [
            f"Run Calls: {self.run_calls}",
            f"Cache Hits: {self.cache_hits}",
            """Tag               Input  Output  Thinking""",
        ]
        for tag, (i, o, th) in self.tokens.items():
            lines.append(f"{tag:<18} {i:5} {o:6} {th:8}")
        return "\n".join(lines)

    # ---------------------------------------------------------------------
    # Internal helpers for persisting state
    # ---------------------------------------------------------------------
    def _load_state(self):
        today = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
        if self._state_path.is_file():
            try:
                data = json.loads(self._state_path.read_text())
            except Exception:
                data = {}
        else:
            data = {}
        if data.get("date") != today:
            self.state = {"date": today, "used": {}, "dead": []}
        else:
            self.state = data
        # Ensure keys exist
        self.state.setdefault("used", {})
        self.state.setdefault("dead", [])
        self.state["date"] = today

    def _save_state(self):
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(self.state, indent=2))
