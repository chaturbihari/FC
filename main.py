from keepAlive import keep_alive
import asyncio
from filmy import main

keep_alive()
asyncio.run(main())
