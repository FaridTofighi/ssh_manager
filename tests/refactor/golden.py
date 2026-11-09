"""Golden files of the characterization tests (``tests/refactor/golden/*.json``).

``check(test, name, value)`` compares ``value`` with the committed golden. With
``OPHION_UPDATE_GOLDENS=1`` it writes the golden instead (done once, on the unmodified baseline; a golden
changed later is a behaviour change and needs a reason in the phase report).
"""

import json
import os
from pathlib import Path

GOLDEN = Path(__file__).with_name("golden")
SECRET = "<SECRET>"


def mask(value, secrets):
    """``value`` (JSON-like) with every secret string replaced by :data:`SECRET`."""

    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, SECRET)
        return value
    if isinstance(value, list):
        return [mask(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: mask(item, secrets) for key, item in value.items()}
    return value


def check(test, name: str, value) -> None:
    path = GOLDEN / f"{name}.json"
    text = json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("OPHION_UPDATE_GOLDENS") == "1":
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    test.assertTrue(path.exists(), f"no golden {path.name}: run once with OPHION_UPDATE_GOLDENS=1 on the baseline")
    test.assertEqual(json.loads(path.read_text(encoding="utf-8")), json.loads(text), f"differs from {path.name}")
