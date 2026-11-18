"""Write the Action API tool definitions to api_schema.json.

  blender -b --factory-startup --python tools/export_api_schema.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from blender_action_recorder import api  # noqa: E402

out = os.path.join(ROOT, "api_schema.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(api.schema(), f, indent=2, ensure_ascii=False)
print(f"wrote {len(api.ACTIONS)} actions to {out}")
