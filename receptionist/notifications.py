"""Durable at-least-once staff alerts; no credentials or callers in logs."""
import asyncio
import json
import logging

import httpx


async def deliver_pending(calendar, token, chat_id):
    if not token or not chat_id:
        return 0
    with calendar.connect() as db:
        rows = db.execute('SELECT id,body FROM outbox WHERE sent=0 ORDER BY created LIMIT 20').fetchall()
    delivered = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for row in rows:
            data = json.loads(row['body'])
            message = f"{data['business']} — {data['type']}\nID: {data['id']}\n" + '\n'.join(f'{k}: {v}' for k,v in data['details'].items())
            success = False
            try:
                response = await client.post(f'https://api.telegram.org/bot{token}/sendMessage', json={'chat_id':chat_id,'text':message[:4000]})
                success = response.is_success and response.json().get('ok') is True
            except (httpx.HTTPError, ValueError):
                logging.getLogger(__name__).warning('Staff notification failed; retained for retry')
            with calendar.connect(write=True) as db:
                db.execute('UPDATE outbox SET attempts=attempts+1,sent=? WHERE id=?', (int(success),row['id']))
            delivered += int(success)
    return delivered


async def notification_worker(calendar, token, chat_id):
    while True:
        try:
            await deliver_pending(calendar, token, chat_id)
        except Exception:
            logging.getLogger(__name__).warning('Notification worker will retry')
        await asyncio.sleep(30)
