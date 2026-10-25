"""Tiny local SMTP server that appends every received message to a JSON-lines file."""
import asyncio, json, sys
from aiosmtpd.controller import Controller


class Handler:
    async def handle_DATA(self, server, session, envelope):
        with open(sys.argv[1], "a") as f:
            f.write(json.dumps({"to": envelope.rcpt_tos, "data": envelope.content.decode("utf8", "replace")}) + "\n")
        return "250 OK"


controller = Controller(Handler(), hostname="127.0.0.1", port=int(sys.argv[2]) if len(sys.argv) > 2 else 51025)
controller.start()
print("smtp sink up", flush=True)
asyncio.new_event_loop().run_forever()
