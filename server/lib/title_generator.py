"""Generate a 3-7 word session title using Claude Haiku via Azure Foundry."""

import json
import os
from urllib import request as urlrequest
from urllib.error import HTTPError

TITLE_PROMPT = """\
Generate a concise 3-7 word title for this Claude Code session based on the user's request below. \
The title should capture the core task. No quotes, no punctuation at the end, no prefix like "Title:". \
Just the title itself.

User's request:
{content}

Title:"""

TITLE_MAX_TOKENS = 30
HTTP_TIMEOUT_S = 6
DEFAULT_TITLE_MODEL = "claude-haiku-4-5-20251001"

# Cache the auth style index that last succeeded so subsequent calls in the
# same function invocation skip the 401/403 probing round-trips.
_last_working_auth = 0


def generate_title(first_user_message):
    """Call Azure Foundry to generate a short session title.

    Returns (title_str, model_used) or (None, None) on failure.
    """
    global _last_working_auth
    endpoint = os.environ.get("AZURE_FOUNDRY_ENDPOINT")
    api_key = os.environ.get("AZURE_FOUNDRY_API_KEY")
    model = os.environ.get("AZURE_FOUNDRY_TITLE_MODEL", DEFAULT_TITLE_MODEL)
    if not endpoint or not api_key:
        return None, None

    text = (first_user_message or "").strip()
    if not text:
        return None, None
    if len(text) > 2000:
        text = text[:2000] + "..."

    body = json.dumps({
        "model": model,
        "max_tokens": TITLE_MAX_TOKENS,
        "messages": [
            {"role": "user", "content": TITLE_PROMPT.format(content=text)}
        ],
    }).encode("utf-8")

    auth_styles = [
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        {"api-key": api_key, "anthropic-version": "2023-06-01"},
        {"Authorization": f"Bearer {api_key}", "anthropic-version": "2023-06-01"},
    ]
    # Try the last-known-good auth style first, then the rest.
    order = list(range(len(auth_styles)))
    if _last_working_auth:
        order.remove(_last_working_auth)
        order.insert(0, _last_working_auth)

    for idx in order:
        headers_auth = auth_styles[idx]
        headers = {"Content-Type": "application/json", **headers_auth}
        req = urlrequest.Request(endpoint, data=body, method="POST", headers=headers)
        try:
            with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
                content = data.get("content") or []
                parts = [
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                title = " ".join(parts).strip()
                if title:
                    _last_working_auth = idx
                    return title, model
        except HTTPError as e:
            if e.code in (401, 403):
                continue
            return None, None
        except Exception:
            return None, None

    return None, None
