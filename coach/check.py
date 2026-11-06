"""Check the Anthropic setup the way the service will see it.

    python -m coach.check /etc/running-coach.env     # read the service's env file
    python -m coach.check                            # use the current environment

Makes one tiny API call (a few tokens, a fraction of a cent) and says exactly what's wrong if it fails.
"""
from __future__ import annotations

import os
import sys


def read_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v
    return out


def problems_with_key(key: str) -> list[str]:
    out = []
    if not key:
        return ["ANTHROPIC_API_KEY is empty or missing."]
    if key != key.strip():
        out.append("The key has leading/trailing whitespace.")
    if key[:1] in "\"'" or key[-1:] in "\"'":
        out.append("The key is wrapped in quotes — remove them.")
    if any(c.isspace() for c in key.strip()):
        out.append("The key contains a space or line break in the middle (bad paste?).")
    if not key.strip(" \"'").startswith("sk-ant-"):
        out.append("Note: Anthropic API keys normally start with “sk-ant-”; this one doesn't (fine if the API accepts it).")
    return out


def main(argv: list[str]) -> int:
    env = dict(os.environ)
    if len(argv) > 1:
        try:
            env.update(read_env_file(argv[1]))
        except OSError as e:
            print(f"✗ Can't read {argv[1]}: {e}")
            return 2
    key = env.get("ANTHROPIC_API_KEY", "")
    model = env.get("COACH_MODEL", "claude-sonnet-5-5")
    effort = env.get("COACH_EFFORT", "medium")

    shown = f"{key.strip()[:7]}…({len(key)} chars)" if key else "(not set)"
    print(f"Key:   {shown}\nModel: {model}")
    bad = problems_with_key(key)
    for b in bad:
        print(f"{'·' if b.startswith('Note:') else '✗'} {b}")
    fatal = [b for b in bad if not b.startswith("Note:")]
    if not key.strip():
        return 1

    import anthropic

    from .errors import explain

    clean = key.strip().strip("\"'")
    try:
        r = anthropic.Anthropic(api_key=clean, timeout=30).messages.create(
            model=model, max_tokens=32, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
        )
    except Exception as e:  # noqa: BLE001 - we want to report anything
        print(f"✗ API call failed: {explain(e) or e}")
        return 1
    print(f"✓ Anthropic accepted the key and model (stop_reason={r.stop_reason}).")
    if fatal:
        print("  …but fix the problems above: the service passes the value to Anthropic exactly as written.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
