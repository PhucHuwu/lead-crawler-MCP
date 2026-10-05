"""Normalization for user-facing names that are looked up in config files.

Profile names are typed by hand on the command line and written by hand in YAML,
so the two spellings drift: someone writes ``singapore_tech`` in the file and
types ``singapore-tech`` at the shell. Nobody remembers which separator a file
used, and a miss here costs a failed run whose error message helpfully lists
``singapore_tech`` while the user is looking at ``singapore-tech``.

Case is already matched loosely by both profile loaders. This extends the same
forgiveness to the separator, which is the other half of the same problem.
"""

from __future__ import annotations

import re

#: Anything a person might use to join the words of a name.
_SEPARATORS = re.compile(r"[\s_-]+")


def profile_key(name: str) -> str:
    """Normalize a profile name for storage and lookup.

    ``Singapore Tech``, ``singapore_tech`` and ``singapore-tech`` all normalize
    to ``singapore_tech``, so they reach the same profile.
    """
    return _SEPARATORS.sub("_", name.strip().casefold())
