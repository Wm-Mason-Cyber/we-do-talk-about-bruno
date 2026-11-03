"""
Production entry point: the Roster API with the live dashboard bolted on.

    uvicorn app:app --host 127.0.0.1 --port 8000 --proxy-headers

Students who want to run just the API locally use `uvicorn roster_api:app` instead.
"""

import dashboard
import roster_api

app = roster_api.app
dashboard.attach(
    app,
    get_roster=lambda: roster_api.students_db,
    reset_roster=roster_api.reset_roster,
)
