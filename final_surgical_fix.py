import os
import re

def wrap_call(content, pattern, wrapper_start, wrapper_end):
    new_content = ""
    pos = 0
    while True:
        match = re.search(pattern, content[pos:])
        if not match:
            new_content += content[pos:]
            break

        start = pos + match.start()
        # Check if it's a definition
        prefix = content[max(0, start-4):start]
        if 'def ' in prefix:
            new_content += content[pos:start+match.end()]
            pos = start + match.end()
            continue

        new_content += content[pos:start]

        # Check if already wrapped
        if content[max(0, start-len(wrapper_start)):start] == wrapper_start:
            new_content += content[start:start+match.end()]
            pos = start + match.end()
            continue

        # Find matching closing paren
        depth = 1
        i = start + match.end()
        while i < len(content) and depth > 0:
            if content[i] == '(': depth += 1
            elif content[i] == ')': depth -= 1
            i += 1

        if depth == 0:
            call = content[start:i]
            new_content += wrapper_start + call + wrapper_end
            pos = i
        else:
            new_content += content[start:start+match.end()]
            pos = start + match.end()

    return new_content

files_to_fix = [
    'tests/test_react.py',
    'tests/test_observation.py',
    'tests/test_cli.py',
    'tests/test_control_plane.py',
    'tests/test_native_tool_calling.py',
    'tests/test_resilience.py',
    'tests/test_react_routing.py',
    'tests/test_shadow.py',
    'tests/test_tracing.py',
    'tests/test_tracing_step41.py',
    'tests/test_trust_integration.py',
    'tests/test_poller.py',
    'tests/test_safety_remediation.py',
    'tests/test_schema_validation.py',
    'tests/test_operational_hardening_remaining.py'
]

for path in files_to_fix:
    if not os.path.exists(path): continue
    with open(path, 'r') as f:
        content = f.read()

    orig = content
    content = wrap_call(content, r'\breact_run\(', 'asyncio.run(', ')')
    content = wrap_call(content, r'\brun\(', 'asyncio.run(', ')')
    content = wrap_call(content, r'\bobs\.run\(', 'asyncio.run(', ')')
    content = wrap_call(content, r'\bcycle\.run\(', 'asyncio.run(', ')')
    content = wrap_call(content, r'\bObservationCycle\.run\(', 'asyncio.run(', ')')

    if content != orig:
        if 'import asyncio' not in content:
            content = 'import asyncio\n' + content
        with open(path, 'w') as f:
            f.write(content)
