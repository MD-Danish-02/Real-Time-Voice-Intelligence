# Calendar
`CalendarProvider`: availability, book (optional Meet link), cancel. Mock (tested) and Google Calendar (REST, untested live). A meeting is stored as `booked` only after the provider returns an event id. Known gap: Google OAuth callback/token storage per tenant is not implemented, so agent tools and `/meetings` use the mock unless you extend `integrations/google`.
