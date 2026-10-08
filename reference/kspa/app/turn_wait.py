"""Keep recognized hesitation turns quiet without delaying normal answers."""
import asyncio
import json
import logging
import re
from contextlib import suppress

HESITATION_WAIT_SECONDS = 5.0


def is_hesitation(text: str) -> bool:
    words = re.findall(r"[^\W\d_]+", text.casefold())
    if not words:
        return True
    if " ".join(words) in {
        "let me check", "let me think", "one moment", "just a moment",
        "hold on", "wait", "等等", "等一下", "让我看看",
    }:
        return True
    return all(re.fullmatch(r"(?:u+h+|u+m+|h+m+|e+r+|a+h*|а+|э+|嗯+|呃+|啊+)", w)
               for w in words)


class CallerTurnGate:
    def __init__(self, websocket, delay=HESITATION_WAIT_SECONDS):
        self.websocket = websocket
        self.delay = delay
        self.pending = None
        self.latest_item = ""

    def cancel(self):
        if self.pending is not None:
            self.pending.cancel()

    def speech_started(self, item_id):
        self.latest_item = item_id
        self.cancel()

    async def transcript(self, item_id, text):
        # An older transcription may arrive after the caller starts another turn.
        if self.latest_item and item_id and item_id != self.latest_item:
            return
        self.cancel()
        if is_hesitation(text):
            self.pending = asyncio.create_task(self._hello_after_wait())
        else:
            await self.websocket.send(json.dumps({"type": "response.create"}))

    async def _hello_after_wait(self):
        try:
            # Starts after transcription, so never less than 5 seconds of silence.
            await asyncio.sleep(self.delay)
            await self.websocket.send(json.dumps({
                "type": "response.create",
                "response": {
                    "tool_choice": "none",
                    "instructions": "The caller has paused. Say only 'Hello?' gently. "
                    "Do not add any other words or repeat the previous question.",
                },
            }))
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("ai-receptionist").exception("Hesitation check-in failed")

    async def close(self):
        self.cancel()
        if self.pending is not None:
            with suppress(asyncio.CancelledError):
                await self.pending
