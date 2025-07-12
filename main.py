from keepAlive import keep_alive
import asyncio
from filmy import main as filmy_main

keep_alive()  # Starts webserver for Render

async def wrapper():
    await filmy_main()

# Don't use asyncio.run here!
loop = asyncio.get_event_loop()
loop.create_task(wrapper())
loop.run_forever()
