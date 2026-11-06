"""Turn low-level Anthropic/network failures into messages a person can act on."""
from __future__ import annotations

import anthropic


class CoachUnavailable(RuntimeError):
    """The coach can't be used as configured (e.g. no API key). The message is shown to the user as-is."""


def explain(e: Exception) -> str | None:
    """A friendly, actionable message for known failures, or None if we don't recognise the error."""
    if isinstance(e, CoachUnavailable):
        return str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return ("Anthropic rejected the API key (401). Re-enter it with “bash deploy/reconfigure.sh” — paste just the key, "
                "no quotes or spaces — and make sure the app restarted afterwards. "
                "Run “python -m coach.check /etc/running-coach.env” to test it.")
    if isinstance(e, anthropic.PermissionDeniedError):
        return "This API key isn't allowed to use that model or feature (403). Check the key's workspace and the COACH_MODEL setting."
    if isinstance(e, anthropic.NotFoundError):
        return "Anthropic couldn't find the model. Check the COACH_MODEL setting (e.g. claude-sonnet-5-5)."
    if isinstance(e, anthropic.RateLimitError):
        return "Anthropic's rate limit was hit. Wait a minute and try again."
    if isinstance(e, anthropic.BadRequestError):
        msg = getattr(e, "message", str(e))
        if "credit balance" in msg.lower():
            return "Your Anthropic account is out of credit. Add credit under Billing in the Anthropic Console, then try again."
        return f"Anthropic rejected the request: {msg}"
    if isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return "Couldn't reach Anthropic. Check that the container has internet access and working DNS."
    if isinstance(e, anthropic.APIStatusError) and getattr(e, "status_code", 0) >= 500:
        return f"Anthropic is having trouble right now ({e.status_code}). Try again shortly."
    return None
