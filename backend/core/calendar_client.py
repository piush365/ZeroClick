"""
core/calendar_client.py
-----------------------
Handles all Google Calendar operations for ZeroClick.
- create_meeting()        : creates a calendar event with Meet link
- check_for_duplicate()   : checks if a meeting already exists at that time
"""

import os
import pickle
from datetime import datetime, timedelta
import pytz
from dataclasses import dataclass
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Only need calendar access
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Paths
BASE_DIR     = os.path.dirname(__file__)
CREDS_FILE   = os.path.join(BASE_DIR, "credentials.json")
TOKEN_FILE   = os.path.join(BASE_DIR, "token.pickle")


@dataclass
class CalendarEvent:
    event_id: str
    title: str
    start: str
    end: str
    meet_link: str
    calendar_link: str
    attendees: list[str]


def _get_calendar_service():
    """
    Authenticate and return a Google Calendar API service object.
    First run: opens browser for OAuth consent.
    After that: uses saved token.pickle automatically.
    """
    creds = None

    # Load saved token if it exists
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    # If no valid token, get one
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Silently refresh expired token
            creds.refresh(Request())
        else:
            # First time — open browser for consent
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token for next time
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("calendar", "v3", credentials=creds)


def create_meeting(
    title: str,
    date: str,
    start_time: str,
    end_time: str,
    attendees: list[str],
    timezone: str = "Asia/Kolkata",
) -> CalendarEvent | None:
    """
    Create a Google Calendar event with a Google Meet link.

    date       : YYYY-MM-DD
    start_time : HH:MM (24h)
    end_time   : HH:MM (24h)
    attendees  : list of email addresses
    """
    try:
        service = _get_calendar_service()

        # Build datetime strings
        start_dt = f"{date}T{start_time}:00"
        end_dt   = f"{date}T{end_time}:00"

        event_body = {
            "summary": title,
            "start": {
                "dateTime": start_dt,
                "timeZone": timezone,
            },
            "end": {
                "dateTime": end_dt,
                "timeZone": timezone,
            },
            "attendees": [{"email": a} for a in attendees],
            "conferenceData": {
                "createRequest": {
                    "requestId": f"zeroclik-{date}-{start_time}".replace(":", "-"),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }

        event = service.events().insert(
            calendarId="primary",
            body=event_body,
            conferenceDataVersion=1,  # required for Meet link
            sendUpdates="all",        # sends email invites to attendees
        ).execute()

        # Extract Meet link
        meet_link = ""
        conf = event.get("conferenceData", {})
        for ep in conf.get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                meet_link = ep.get("uri", "")
                break

        print(f"[Calendar] Event created: {event.get('htmlLink')}")

        return CalendarEvent(
            event_id      = event["id"],
            title         = event["summary"],
            start         = event["start"]["dateTime"],
            end           = event["end"]["dateTime"],
            meet_link     = meet_link,
            calendar_link = event.get("htmlLink", ""),
            attendees     = attendees,
        )

    except Exception as e:
        print(f"[Calendar] Error creating event: {e}")
        return None


def check_for_duplicate(
    attendees: list[str],
    date: str,
    start_time: str,
    timezone: str = "Asia/Kolkata",
) -> bool:
    """
    Check if a meeting already exists at this time to avoid double-booking.
    Returns True if a duplicate exists, False if the slot is free.
    """
    try:
        service = _get_calendar_service()

        # Convert local time to UTC for the API query
        tz = pytz.timezone(timezone)
        dt_local = tz.localize(datetime.fromisoformat(f"{date}T{start_time}:00"))
        dt_utc = dt_local.astimezone(pytz.utc)
        dt_utc_end = dt_utc + timedelta(hours=1)
        time_min = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = dt_utc_end.strftime("%Y-%m-%dT%H:%M:%SZ")

        result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = result.get("items", [])
        if events:
            print(f"[Calendar] Duplicate found: {events[0].get('summary')}")
            return True

        return False

    except Exception as e:
        print(f"[Calendar] Error checking duplicates: {e}")
        return False