from __future__ import annotations

import sys
import traceback
from pathlib import Path

import modal

APP_DIR = Path(__file__).resolve().parent


def _ignore(path: Path) -> bool:
    parts = path.parts
    if ".venv" in parts or "__pycache__" in parts or path.name == "apify.md":
        return True
    return path.suffix not in {".py", ".md", ".yaml"}


image = (
    modal.Image.debian_slim("3.12")
    .pip_install(
        "apify-client",
        "google-api-python-client",
        "google-auth",
        "google-auth-oauthlib",
        "google-auth-httplib2",
        "httpx",
        "trafilatura",
        "beautifulsoup4",
        "PyYAML",
        "dnspython",
    )
    .add_local_dir(str(APP_DIR), remote_path="/app", copy=True, ignore=_ignore)
)

app = modal.App("ppc-outreach")

secrets = [modal.Secret.from_name("ppc-outreach-secrets")]


def _setup():
    sys.path.insert(0, "/app")


def _guarded(component: str, callback):
    _setup()
    import ops

    try:
        callback()
        return True
    except Exception as exc:
        traceback.print_exc()
        try:
            ops.alert(component, f"{type(exc).__name__}: {exc}")
        except Exception:
            traceback.print_exc()
        return False


# The Modal workspace plan allows five scheduled functions in total and this app
# spends them on the single */5 `tick` cron plus its own daily jobs: each daily
# job fires on the first tick at or after its time, guarded by the Ops tab (one
# row per component per day), so a retried or overlapping container cannot
# double-run it.
DAILY_JOBS = (("monitor", (19, 0)),)


def _daily_jobs_due(ops) -> list[str]:
    """The daily jobs whose time has come and which have not run yet today."""
    now = ops.now_local()
    ripe = [name for name, at in DAILY_JOBS if (now.hour, now.minute) >= at]
    if not ripe:
        return []
    done = ops.ran_today(*[name for name, _ in DAILY_JOBS])
    return [name for name in ripe if name not in done]


@app.function(image=image, secrets=secrets, schedule=modal.Cron("*/5 * * * *"), timeout=7200, max_containers=1)
def tick():
    _setup()
    import main
    import ops

    _guarded("poll", main.run_poll)
    _guarded("outreach", main.run_outreach)
    try:
        due = _daily_jobs_due(ops)
    except Exception:
        traceback.print_exc()
        due = []
    for component in due:
        _guarded(component, lambda: main.run_daily_check(send=True))


if __name__ == "__main__":
    app.run()