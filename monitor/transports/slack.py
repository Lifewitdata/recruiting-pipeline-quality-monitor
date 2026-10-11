"""Slack incoming-webhook adapter.

Stdlib only - an alert path should not depend on a package that can fail to install
the night it matters. The URL is the whole configuration; the token never lives in
this repository.

Tested against a local HTTP sink, not a workspace: `tests/test_alerting.py` starts a
server on 127.0.0.1, points this adapter at it, and asserts on the JSON that arrives.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from monitor.alerts import AlertMessage
from monitor.transports.base import Receipt

ENV_VAR = "SLACK_WEBHOOK_URL"
CHANNEL_LIMIT = 3000  # Slack truncates an over-long section block


@dataclass
class SlackWebhookTransport:
    url: str
    timeout: float = 5.0
    name: str = "slack"

    @classmethod
    def from_env(cls, env: dict) -> "SlackWebhookTransport | None":
        url = env.get(ENV_VAR)
        return cls(url) if url else None

    def payload(self, message: AlertMessage) -> dict:
        emoji = {"critical": ":rotating_light:", "warning": ":warning:"}.get(
            message.severity, ":information:"
        )
        colour = {"critical": "#B4231C", "warning": "#B54708"}.get(message.severity, "#475467")
        return {
            "text": f"{emoji} {message.subject}",
            "attachments": [
                {
                    "color": colour,
                    "mrkdwn_in": ["text"],
                    "text": _clip(message.text, CHANNEL_LIMIT),
                    "footer": f"recruiting-funnel-monitor | run {message.run_id}",
                }
            ],
        }

    def send(self, message: AlertMessage) -> Receipt:
        body = json.dumps(self.payload(message)).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.getcode()
                text = response.read().decode("utf-8", "replace")[:200]
        except urllib.error.HTTPError as exc:
            return Receipt(self.name, False, f"HTTP {exc.code}: {exc.reason}")
        except (urllib.error.URLError, OSError) as exc:
            return Receipt(self.name, False, f"transport error: {exc}")
        if status not in (200, 201, 202, 204):
            return Receipt(self.name, False, f"HTTP {status}: {text}")
        return Receipt(self.name, True, f"HTTP {status}: {text or 'ok'}")


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
