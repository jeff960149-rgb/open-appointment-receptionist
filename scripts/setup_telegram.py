"""Explicit setup action; never runs automatically on application startup."""
import os
import sys

from dotenv import load_dotenv
import httpx

load_dotenv()
token=os.getenv('TELEGRAM_BOT_TOKEN','')
secret=os.getenv('TELEGRAM_WEBHOOK_SECRET','')
base=os.getenv('PUBLIC_BASE_URL','').rstrip('/')
if not token or len(secret)<24 or not base.startswith('https://'):
    sys.exit('Set TELEGRAM_BOT_TOKEN, a 24+ character TELEGRAM_WEBHOOK_SECRET, and HTTPS PUBLIC_BASE_URL.')
try:
    response=httpx.post(f'https://api.telegram.org/bot{token}/setWebhook',json={
        'url':base+'/telegram/webhook','secret_token':secret,'allowed_updates':['message']},timeout=15)
    if not response.is_success or not response.json().get('ok'):
        sys.exit('Webhook setup rejected. Check the bot account and HTTPS endpoint.')
except httpx.HTTPError:
    sys.exit('Could not reach Telegram; no credentials printed.')
print('Telegram webhook configured.')
