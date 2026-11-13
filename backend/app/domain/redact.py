"""Redaction boundary for sensitive remote-artifact references.

Remote image/video providers commonly return temporary, signed or
token-bearing URLs for artifact retrieval. Those URLs are bearer
references — durable storage must keep them (a future reconcile or
retrieval workflow needs the exact value), but they must never leave
the process through job-detail API responses, websocket broadcasts, or
user-visible strings. Dependency-free so any layer can import it.
"""

from __future__ import annotations

from typing import Any

REDACTED = "[redacted — sensitive remote reference]"

# Keys whose values are remote artifact references (signed/temporary
# URLs are bearer-capable) or raw artifact payloads (unbounded binary
# material). Key presence is preserved on output — callers can still
# tell that a remote reference existed without ever seeing it.
SENSITIVE_DETAIL_KEYS = frozenset({
    "url", "image_url", "video_url", "audio_url", "artifact_url",
    "asset_url", "download_url", "signed_url", "remote_url",
    "result_url", "temp_url",
    "image_b64", "b64_json", "image_base64",
})


def redact_detail(obj: Any) -> Any:
    """Deep-copy a JSON-able structure with sensitive values replaced
    by the redaction marker. Recurses through dicts and lists so
    nested detail (attempt history, remote metadata) is covered."""
    if isinstance(obj, dict):
        return {k: (REDACTED if k in SENSITIVE_DETAIL_KEYS
                    else redact_detail(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_detail(v) for v in obj]
    return obj
