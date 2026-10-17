# Telephony
`TelephonyProvider`: `dial`, `hangup`, `transfer`, `get_call`, `parse_inbound_webhook`, `health_check`. Adapters: Mock (tested), Exotel, Plivo (REST), Generic SIP via ARI. Exotel transfer needs an Exoml applet and raises a clear error. Live adapters untested; fill provider credentials in `.env` and test with your own numbers.
