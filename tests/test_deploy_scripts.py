"""Run deploy/reconfigure.sh and install.sh --configure-only for real, in a sandbox, with fake systemctl/curl."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RECONF = ROOT / "deploy" / "reconfigure.sh"
INSTALL = ROOT / "deploy" / "install.sh"

BASE_ENV = """ANTHROPIC_API_KEY=sk-ant-secretkey123456
COACH_MODEL=claude-sonnet-5-5
COACH_EFFORT=medium
PUBLIC_URL=http://192.168.1.50:8000
TZ=America/New_York
ALLOW_SIGNUPS=true
INVITE_CODE=
STRAVA_CLIENT_ID=
STRAVA_CLIENT_SECRET=
"""


def shim(bindir: Path, name: str, body: str):
    p = bindir / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def box(tmp_path):
    env = tmp_path / "app.env"
    env.write_text(BASE_ENV)
    env.chmod(0o640)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    shim(bindir, "systemctl", f'echo "systemctl $*" >> {log}\nexit 0\n')
    shim(bindir, "curl", f'echo "curl $*" >> {log}\nexit "${{FAKE_CURL_EXIT:-0}}"\n')
    return dict(env=env, bindir=bindir, log=log, tmp=tmp_path)


def run(box, *args, stdin="", extra_env=None, script=RECONF):
    e = {**os.environ, "ENV_FILE": str(box["env"]), "PATH": f"{box['bindir']}:{os.environ['PATH']}",
         "HEALTH_TRIES": "1", "CHECK_CMD": "true", **(extra_env or {})}
    return subprocess.run(["bash", str(script), *args], input=stdin, capture_output=True, text=True, env=e, timeout=60)


def val(box, key):
    for line in box["env"].read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]


def test_show_masks_secrets_and_changes_nothing(box):
    r = run(box, "--show")
    assert r.returncode == 0
    assert "sk-ant-secretkey123456" not in r.stdout and "sk-ant-" in r.stdout   # masked, prefix only
    assert "COACH_EFFORT" in r.stdout and "medium" in r.stdout
    assert box["env"].read_text() == BASE_ENV
    assert not list(box["tmp"].glob("app.env.bak.*"))


def test_set_values_backs_up_restarts_and_keeps_permissions(box):
    r = run(box, "COACH_EFFORT=low", "TZ=America/Chicago", "COACH_MODEL=claude-haiku-5-5")
    assert r.returncode == 0, r.stderr
    assert (val(box, "COACH_EFFORT"), val(box, "TZ"), val(box, "COACH_MODEL")) == ("low", "America/Chicago", "claude-haiku-5-5")
    assert val(box, "ANTHROPIC_API_KEY") == "sk-ant-secretkey123456"            # untouched
    assert "COACH_EFFORT" in r.stdout and "medium  ->  low" in r.stdout
    assert "sk-ant-secretkey123456" not in r.stdout
    backups = list(box["tmp"].glob("app.env.bak.*"))
    assert len(backups) == 1 and backups[0].read_text() == BASE_ENV
    assert oct(box["env"].stat().st_mode & 0o777) == "0o640"
    calls = box["log"].read_text()
    assert "systemctl restart running-coach" in calls and "healthz" in calls
    assert "is up with the new settings" in r.stdout


def test_invalid_values_are_rejected_all_or_nothing(box):
    for bad in ("COACH_EFFORT=extreme", "TZ=Mars/Olympus", "PUBLIC_URL=192.168.1.5:8000", "SYNC_INTERVAL_MIN=abc", "ALLOW_SIGNUPS=maybe"):
        r = run(box, "COACH_MODEL=claude-haiku-5-5", bad)   # a valid change alongside the bad one must not be applied
        assert r.returncode != 0, bad
        assert box["env"].read_text() == BASE_ENV, bad
    assert "restart" not in (box["log"].read_text() if box["log"].exists() else "")


def test_no_changes_means_no_restart_and_no_backup(box):
    r = run(box, "COACH_EFFORT=medium")
    assert r.returncode == 0 and "No changes" in r.stdout
    assert not list(box["tmp"].glob("app.env.bak.*"))
    assert not box["log"].exists() or "restart" not in box["log"].read_text()


def test_unset_removes_a_value(box):
    run(box, "INVITE_CODE=sesame")
    assert val(box, "INVITE_CODE") == "sesame"
    r = run(box, "--unset", "INVITE_CODE")
    assert r.returncode == 0 and val(box, "INVITE_CODE") == ""


def test_interactive_walkthrough_keeps_blank_answers(box):
    # Order of prompts: key, model, effort, url, tz, signups, invite, strava id, strava secret, interval, auto-adjust
    answers = ["", "", "high", "", "", "false", "new", "", "", "", ""]
    r = run(box, stdin="\n".join(answers) + "\n")
    assert r.returncode == 0, r.stderr
    assert val(box, "COACH_EFFORT") == "high" and val(box, "ALLOW_SIGNUPS") == "false"
    assert val(box, "COACH_MODEL") == "claude-sonnet-5-5" and val(box, "ANTHROPIC_API_KEY") == "sk-ant-secretkey123456"
    assert len(val(box, "INVITE_CODE")) == 8                      # 'new' generated one


def test_interactive_invalid_answer_fails_cleanly_without_tty(box):
    r = run(box, stdin="\n\nbanana\n")
    assert r.returncode != 0 and "COACH_EFFORT must be" in (r.stdout + r.stderr)
    assert val(box, "COACH_EFFORT") == "medium"


def test_unhealthy_after_change_rolls_back_automatically(box):
    r = run(box, "COACH_EFFORT=high", extra_env={"FAKE_CURL_EXIT": "22"})
    assert r.returncode == 1
    assert "Restoring the previous config" in r.stderr
    assert box["env"].read_text() == BASE_ENV                      # rolled back
    assert box["log"].read_text().count("systemctl restart") == 2  # restarted again on the old config


def test_refuses_when_not_installed(box):
    box["env"].unlink()
    r = run(box, "COACH_EFFORT=low")
    assert r.returncode != 0 and "not installed" in r.stderr.lower() or "not found" in r.stderr.lower()


def test_no_restart_flag_only_writes_config(box):
    r = run(box, "--no-restart", "COACH_EFFORT=low")
    assert r.returncode == 0 and val(box, "COACH_EFFORT") == "low"
    assert not box["log"].exists()


def test_only_five_backups_are_kept(box):
    for i in range(8):
        # distinct second-resolution names would collide, so create synthetic old backups too
        (box["tmp"] / f"app.env.bak.2020010{i}-000000").write_text("old")
    run(box, "COACH_EFFORT=low")
    assert len(list(box["tmp"].glob("app.env.bak.*"))) <= 5


def test_install_configure_only_still_works_after_refactor(box, tmp_path):
    fresh = tmp_path / "fresh.env"
    r = subprocess.run(["bash", str(INSTALL), "--configure-only"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={**os.environ, "ENV_FILE": str(fresh), "DATA_DIR": str(tmp_path / "d"), "ANTHROPIC_API_KEY": "k", "REQUIRE_INVITE": "1"})
    assert r.returncode == 0, r.stderr
    text = fresh.read_text()
    assert "ANTHROPIC_API_KEY=k" in text and "INVITE_CODE=" in text and len(text.split("INVITE_CODE=")[1].split()[0]) == 8


def test_pasted_key_is_cleaned_and_bad_keys_rejected(box):
    r = run(box, 'ANTHROPIC_API_KEY=  "sk-ant-api03-newkey"  ')          # spaces + wrapping quotes
    assert r.returncode == 0, r.stderr
    assert val(box, "ANTHROPIC_API_KEY") == "sk-ant-api03-newkey"
    before = box["env"].read_text()
    r = run(box, "ANTHROPIC_API_KEY=sk-ant-has space inside")
    assert r.returncode != 0 and box["env"].read_text() == before        # a space inside is always a paste error
    r = run(box, "ANTHROPIC_API_KEY=some-other-format-key-123")          # unusual prefix: warn, but don't lock the user out
    assert r.returncode == 0 and val(box, "ANTHROPIC_API_KEY") == "some-other-format-key-123"
    assert "normally start with sk-ant-" in (r.stdout + r.stderr)


def test_changing_key_or_model_triggers_the_check_but_other_changes_do_not(box):
    marker = box["tmp"] / "checked"
    chk = {"CHECK_CMD": f"touch {marker}"}
    run(box, "COACH_EFFORT=low", extra_env=chk)
    assert not marker.exists()                                           # unrelated change: no API test
    run(box, "COACH_MODEL=claude-haiku-5-5", extra_env=chk)
    assert marker.exists()                                               # model change: tested
    marker.unlink()
    run(box, "ANTHROPIC_API_KEY=sk-ant-another-key-999", "--no-check", extra_env=chk)
    assert not marker.exists()                                           # --no-check respected
    run(box, "ANTHROPIC_API_KEY=sk-ant-third-key-000", extra_env=chk)
    assert marker.exists()


def test_check_flag_only_runs_the_check(box):
    marker = box["tmp"] / "checked"
    r = run(box, "--check", extra_env={"CHECK_CMD": f"touch {marker}"})
    assert r.returncode == 0 and marker.exists()
    assert box["env"].read_text() == BASE_ENV and not box["log"].exists()   # nothing changed or restarted
    assert run(box, "--check", extra_env={"CHECK_CMD": "false"}).returncode != 0
