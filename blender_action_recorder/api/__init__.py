"""Action API: the action space shared by human recordings and models.

    from blender_action_recorder import api
    api.execute({"name": "set_keys", "args": {...}})
    api.schema()            # tool definitions (JSON schema)

Layers implemented so far:
  edit     (core.py)     low-level, complete: set_keys, delete_keys, set_key_props, ...
  observe  (observe.py)  list_channels, get_keys, sample_curves, sample_world, detect_issues, ...
The translator (translate.py) turns recorded steps into edit calls.
"""

from . import core, observe  # noqa: F401  (registers actions)
from .registry import ACTIONS, ApiError, execute, schema  # noqa: F401
