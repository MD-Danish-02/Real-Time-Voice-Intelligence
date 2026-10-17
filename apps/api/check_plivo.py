import asyncio
import httpx
from app.core.config import settings

async def check_plivo_calls():
    auth = (settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f'https://api.plivo.com/v1/Account/{settings.PLIVO_AUTH_ID}/Call/', auth=auth)
        print(f'Status: {resp.status_code}')
        if resp.status_code == 200:
            data = resp.json()
            print(f'Total: {data.get("meta", {}).get("total_count", 0)}')
            for call in data.get('objects', [])[:5]:
                call_uuid = call.get('call_uuid')
                call_state = call.get('call_state')
                to_num = call.get('to')
                from_num = call.get('from')
                direction = call.get('direction')
                print(f'Call: {call_uuid} - State: {call_state} - To: {to_num} - From: {from_num} - Direction: {direction}')
        else:
            print(f'Error: {resp.text}')

asyncio.run(check_plivo_calls())