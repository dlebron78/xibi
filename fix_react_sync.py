import os

with open('xibi/react.py', 'r') as f:
    content = f.read()

content = content.replace('async def run(', 'async def _run_async(')
content = content.replace('async def dispatch(', 'async def _dispatch_async(')

# Update calls to dispatch within react.py
content = content.replace('await dispatch(', 'await _dispatch_async(')

# Add sync wrappers
wrapper = """
def dispatch(*args, **kwargs):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        # This is the tricky part. If we are already in a loop, we can't use run().
        # But for Xibi, we expect these calls to be from top-level or threads.
        import nest_asyncio
        nest_asyncio.apply()
        return asyncio.run(_dispatch_async(*args, **kwargs))
    else:
        return asyncio.run(_dispatch_async(*args, **kwargs))

def run(*args, **kwargs):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        import nest_asyncio
        nest_asyncio.apply()
        return asyncio.run(_run_async(*args, **kwargs))
    else:
        return asyncio.run(_run_async(*args, **kwargs))
"""

with open('xibi/react.py', 'w') as f:
    f.write(content + wrapper)
