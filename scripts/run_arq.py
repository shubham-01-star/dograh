import asyncio
import sys
from arq.cli import cli

if __name__ == '__main__':
    # Create and set a new event loop in the main thread for Python 3.12+ compatibility
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Run the standard ARQ CLI with command line arguments
    cli()
