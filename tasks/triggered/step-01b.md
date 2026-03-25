# Step 01b — Fix CI Failures in PR #1

## Context

PR #1 ("Implement Step 1: get_model() Router") is open but all 4 CI checks are failing.
This task fixes those failures on the branch `step-1-get-model-router-1550666350881804579`.

## Failures to Fix

### 1. Missing dev dependency: `responses`
`tests/test_router.py` imports `responses` (HTTP mocking library) but it is not listed in
`pyproject.toml`. Add it to `[project.optional-dependencies] dev`:

```toml
"responses>=0.25",
```

### 2. Missing dev dependency: `types-requests`
`mypy` fails because `requests` has no type stubs bundled. Add to dev deps:

```toml
"types-requests>=2.31",
```

### 3. Deprecated Gemini SDK
`xibi/router.py` uses `import google.generativeai as genai` which is fully deprecated
and will cause CI warnings/failures. Replace with the new SDK:

```python
import google.genai as genai
from google.genai import types as genai_types
```

Update `GeminiClient` accordingly:
- Replace `genai.configure(api_key=...)` with `client = genai.Client(api_key=...)`
- Replace `genai.GenerativeModel(...)` with `client.models`
- Use `client.models.generate_content(model=self.model, contents=prompt)` for generation
- Update structured output to use `genai_types.GenerateContentConfig`

The new package name is `google-genai` (not `google-generativeai`). Update `pyproject.toml`
dependencies accordingly:
- Remove: `"google-generativeai>=0.8"`
- Add: `"google-genai>=1.0"`

### 4. Ruff lint issues in `tests/test_router.py`
Two auto-fixable issues:
- I001: Import block is unsorted — sort imports (stdlib → third-party → local)
- F401: `NoModelAvailableError` is imported but unused — remove it

Correct import block:
```python
import json
import os
from unittest.mock import MagicMock, patch

import pytest
import responses

from xibi.router import (
    ConfigValidationError,
    GeminiClient,
    OllamaClient,
    _check_provider_health,
    _resolve_model,
    get_model,
    load_config,
)
```

## Instructions

1. Check out branch `step-1-get-model-router-1550666350881804579`
2. Make all the fixes above
3. Run `ruff check --fix`, `mypy xibi/`, `pytest tests/test_router.py` locally to verify
4. Push the fixes to the same branch (do NOT create a new PR — amend or add a commit to the existing branch so PR #1 updates)

## Success Criteria

All 4 CI checks pass on PR #1:
- `Xibi CI / lint` — green
- `Xibi CI / typecheck` — green
- `Xibi CI / test` — green
- `Xibi CI / secrets-check` — green
