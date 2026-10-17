import os
os.environ.update({
    'PLIVO_AUTH_ID': 'TEST',
    'PLIVO_AUTH_TOKEN': 'TEST',
    'PLIVO_CALLER_ID': '+15551234567',
    'SIP_PUBLIC_HOST': '161.118.180.20',
    'VOICE_MEDIA_HOST': '100.80.145.109',
    'VOICE_MEDIA_HOST_EXTERNAL': '100.80.145.109',
    'JWT_SECRET': 'test-secret-key-for-testing-only',
    'ENV': 'test',
    'DATABASE_URL': 'sqlite:///test.db',
    'ASYNC_DATABASE_URL': 'sqlite+aiosqlite:///test.db',
    'REDIS_URL': 'redis://localhost:6379/0',
})

from app.core.config import settings
print('SIP_PUBLIC_HOST:', settings.SIP_PUBLIC_HOST)
print('VOICE_MEDIA_HOST:', settings.VOICE_MEDIA_HOST)
print('VOICE_MEDIA_HOST_EXTERNAL:', settings.VOICE_MEDIA_HOST_EXTERNAL)

sip_host = getattr(settings, 'SIP_PUBLIC_HOST', None)
sip_uri = 'sip:voxai_voice_app@' + sip_host if sip_host else None
print('Generated SIP URI:', sip_uri)

xml = '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Dial>\n        <Sip>' + sip_uri + '</Sip>\n    </Dial>\n</Response>'
print('XML:')
print(xml)