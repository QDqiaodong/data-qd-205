"""Alias module for callers that prefer the name ``micro_objects.sampler``."""
from .sampling import *  # noqa: F401,F403
from . import sampling as _sampling

# Re-export public names even if sampling later gains an __all__.
for _name in dir(_sampling):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_sampling, _name)
