"""
The Roster API with the live classroom dashboard attached. Run it with:

    uvicorn app:app --host 0.0.0.0 --port 5000

Students who want to run just the API on their own computer use
`uvicorn roster_api:app --reload` instead.

The teacher key (for the dashboard's reset button) comes from $TEACHER_KEY,
or is generated once and kept in .teacher_key next to this file.
"""

import os
import secrets
from pathlib import Path

import dashboard
import roster_api

HERE = Path(__file__).resolve().parent


def teacher_key() -> str:
    if os.environ.get("TEACHER_KEY"):
        return os.environ["TEACHER_KEY"]
    key_file = HERE / ".teacher_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(9) + "\n")
        key_file.chmod(0o600)
    return key_file.read_text().strip()


KEY = teacher_key()

app = roster_api.app
dashboard.attach(
    app,
    get_roster=lambda: roster_api.students_db,
    reset_roster=roster_api.reset_roster,
    teacher_key=KEY,
    access_log=Path(os.environ.get("ACCESS_LOG", HERE / "logs" / "access.log")),
)

print(f"[bruno-api] Projector dashboard: http://<this-machine>:<port>/dashboard   Teacher key: {KEY}", flush=True)
