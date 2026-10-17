"""Calendar provider abstraction: check availability, book meetings, produce a join link."""
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx

from app.core.config import settings


@dataclass
class TimeSlot:
    start: datetime
    end: datetime


@dataclass
class BookedMeeting:
    external_event_id: str
    join_url: Optional[str]
    start: datetime
    end: datetime


class CalendarProviderError(Exception):
    pass


class CalendarProvider(ABC):
    provider_name: str = "base"

    @abstractmethod
    async def get_authorization_url(self, *, state: str) -> str:
        ...

    @abstractmethod
    async def exchange_code_for_token(self, *, code: str) -> dict:
        ...

    @abstractmethod
    async def check_availability(self, *, access_token: str, day: datetime,
                                  duration_minutes: int = 30) -> list[TimeSlot]:
        ...

    @abstractmethod
    async def book_meeting(self, *, access_token: str, title: str, start: datetime,
                            duration_minutes: int, attendee_email: Optional[str] = None,
                            create_meet_link: bool = True) -> BookedMeeting:
        ...

    @abstractmethod
    async def cancel_meeting(self, *, access_token: str, external_event_id: str) -> None:
        ...


class MockCalendarProvider(CalendarProvider):
    provider_name = "mock"

    async def get_authorization_url(self, *, state: str) -> str:
        return f"/mock-calendar/authorize?state={state}"

    async def exchange_code_for_token(self, *, code: str) -> dict:
        return {"access_token": f"mock-cal-{uuid.uuid4().hex[:10]}", "refresh_token": None}

    async def check_availability(self, *, access_token: str, day: datetime,
                                  duration_minutes: int = 30) -> list[TimeSlot]:
        base = day.replace(hour=10, minute=0, second=0, microsecond=0)
        return [
            TimeSlot(start=base + timedelta(hours=i), end=base + timedelta(hours=i, minutes=duration_minutes))
            for i in range(0, 6)
        ]

    async def book_meeting(self, *, access_token: str, title: str, start: datetime,
                            duration_minutes: int, attendee_email: Optional[str] = None,
                            create_meet_link: bool = True) -> BookedMeeting:
        event_id = f"mock-event-{uuid.uuid4().hex[:10]}"
        return BookedMeeting(
            external_event_id=event_id,
            join_url=f"https://meet.mock.local/{event_id}" if create_meet_link else None,
            start=start, end=start + timedelta(minutes=duration_minutes),
        )

    async def cancel_meeting(self, *, access_token: str, external_event_id: str) -> None:
        return None


class GoogleCalendarProvider(CalendarProvider):
    """Real Google Calendar v3 adapter. Requires GOOGLE_CLIENT_ID/SECRET (OAuth consent screen)."""
    provider_name = "google"

    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    API_URL = "https://www.googleapis.com/calendar/v3"

    async def get_authorization_url(self, *, state: str) -> str:
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise CalendarProviderError("Google Calendar OAuth is not configured on this server.")
        import urllib.parse
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
            "scope": "https://www.googleapis.com/auth/calendar",
            "state": state,
        }
        return f"{self.AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code_for_token(self, *, code: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self.TOKEN_URL, data={
                "code": code, "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI, "grant_type": "authorization_code",
            })
            if resp.status_code >= 400:
                raise CalendarProviderError(f"Google token exchange failed: {resp.text}")
            return resp.json()

    async def refresh_access_token(self, *, refresh_token: str) -> dict:
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise CalendarProviderError("Google Calendar OAuth is not configured on this server.")
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self.TOKEN_URL, data={
                "refresh_token": refresh_token, "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET, "grant_type": "refresh_token",
            })
            if resp.status_code >= 400:
                raise CalendarProviderError(f"Google token refresh failed: {resp.text}")
            data = resp.json()
            data["refresh_token"] = refresh_token
            return data

    async def check_availability(self, *, access_token: str, day: datetime,
                                  duration_minutes: int = 30) -> list[TimeSlot]:
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.API_URL}/freeBusy",
                json={"timeMin": day_start.isoformat(), "timeMax": day_end.isoformat(),
                      "items": [{"id": "primary"}]},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                raise CalendarProviderError(f"Google freeBusy failed: {resp.text}")
            busy = resp.json().get("calendars", {}).get("primary", {}).get("busy", [])
        slots = []
        cursor = day_start.replace(hour=9)
        end_of_day = day_start.replace(hour=18)
        busy_ranges = [(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in busy]
        while cursor + timedelta(minutes=duration_minutes) <= end_of_day:
            slot_end = cursor + timedelta(minutes=duration_minutes)
            if not any(bs < slot_end and be > cursor for bs, be in busy_ranges):
                slots.append(TimeSlot(start=cursor, end=slot_end))
            cursor += timedelta(minutes=duration_minutes)
        return slots

    async def book_meeting(self, *, access_token: str, title: str, start: datetime,
                            duration_minutes: int, attendee_email: Optional[str] = None,
                            create_meet_link: bool = True) -> BookedMeeting:
        end = start + timedelta(minutes=duration_minutes)
        body = {
            "summary": title,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        if attendee_email:
            body["attendees"] = [{"email": attendee_email}]
        if create_meet_link:
            body["conferenceData"] = {"createRequest": {"requestId": str(uuid.uuid4())}}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.API_URL}/calendars/primary/events",
                params={"conferenceDataVersion": 1 if create_meet_link else 0},
                json=body, headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                raise CalendarProviderError(f"Google book_meeting failed: {resp.text}")
            data = resp.json()
            join_url = data.get("hangoutLink") or data.get("htmlLink")
            return BookedMeeting(external_event_id=data["id"], join_url=join_url, start=start, end=end)

    async def cancel_meeting(self, *, access_token: str, external_event_id: str) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(
                f"{self.API_URL}/calendars/primary/events/{external_event_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400 and resp.status_code != 410:
                raise CalendarProviderError(f"Google cancel_meeting failed: {resp.text}")


def get_calendar_provider(provider_name: str) -> CalendarProvider:
    if provider_name == "google":
        return GoogleCalendarProvider()
    if provider_name == "mock":
        return MockCalendarProvider()
    raise ValueError(f"Unknown calendar provider: {provider_name}")
