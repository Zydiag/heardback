"""Deliver real announcer events through the real brain, with a socket recorder."""
import asyncio
import json

from heardback.brain.brain import Brain
from heardback.status import away


class Voice:
    def __init__(self, brain=None):
        self.messages = []
        self.brain = brain

    def is_closing(self):
        return False

    def write(self, data):
        msg = json.loads(data)
        self.messages.append(msg)
        if self.brain and msg.get("delivery_id"):
            asyncio.get_running_loop().create_task(self.brain.handle(
                {"type": "delivery", "id": msg["delivery_id"], "result": "spoken"}, self))

    async def drain(self):
        pass


def due(ann, *, muted=False, at_desk=False, busy=False):
    if muted:
        away.mute(None, ann.now())
    else:
        away.unmute()
    if at_desk:
        away.flag_path().unlink(missing_ok=True)
    else:
        away.flag_path().touch()
    if not hasattr(ann, "brain"):
        ann.brain = Brain(announcer=ann)
        ann.brain.voice = Voice()
    b = ann.brain
    b.voice_busy = busy
    before = len(b.voice.messages)

    async def run():
        for event in ann.due():
            await b.announce(event)
        await b.flush_events()
    asyncio.run(run())
    lines = [m["text"] for m in b.voice.messages[before:] if m["type"] == "say"]
    return [" ".join(lines)] if lines else []
