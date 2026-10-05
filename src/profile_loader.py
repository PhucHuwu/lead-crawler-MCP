"""Shared loading and validation for the named YAML profile files.

Both halves of a profile — search definitions and qualification rules — have the
same shape: a YAML mapping of name to model, read from a file that a person
edits by hand. They therefore fail in the same three ways, and each one is worth
a specific message rather than a traceback:

* a profile with an empty name, which cannot be selected;
* two names that differ only in punctuation (``sea_fintech`` / ``sea-fintech``),
  where the second would silently replace the first;
* a body that does not validate against its model.

Keeping those checks here rather than in each half means a fix lands on both,
instead of on whichever file the person happens to remember.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from src.utils.errors import ConfigError
from src.utils.names import profile_key
from src.utils.yaml_load import load_yaml_mapping

#: A pydantic model that a profile body must validate as.
ProfileT = TypeVar("ProfileT", bound=BaseModel)


def format_validation_error(error: ValidationError) -> str:
    """Render a pydantic error as ``field: message`` pairs for the CLI.

    A field-level failure carries a ``loc``; a model-level one does not, and is
    reported as ``<profile>`` so every line still reads as
    ``something: what went wrong``.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in issue['loc']) or '<profile>'}: {issue['msg']}"
        for issue in error.errors()
    )


def load_named_profiles(
    model: type[ProfileT],
    path: str | Path | None,
    *,
    default_path: Path,
    label: str,
    env_var: str,
) -> dict[str, ProfileT]:
    """Read a ``name: profile`` YAML mapping, validating every profile in it.

    Args:
        model: Model each profile body must validate as.
        path: File to read. ``None`` uses ``default_path``.
        default_path: File used when ``path`` is ``None``.
        label: What the file holds, for error messages (``"search profiles"``).
        env_var: Environment variable that overrides ``default_path``.

    Returns:
        Profile name -> profile. Names are normalized
        (:func:`~src.utils.names.profile_key`) so lookup forgives case and
        separators.

    Raises:
        ConfigError: if the file is missing, is not valid YAML, is not a mapping
            of names to profiles, contains a profile that fails validation, or
            defines two names that differ only in punctuation.
    """
    resolved = Path(path) if path is not None else default_path
    document = load_yaml_mapping(resolved, label=label, env_var=env_var)

    profiles: dict[str, ProfileT] = {}
    spellings: dict[str, str] = {}
    for raw_name, raw_profile in document.items():
        name = str(raw_name).strip()
        if not name:
            raise ConfigError(f"{resolved} has a profile with an empty name")
        key = profile_key(name)
        if key in spellings:
            # Two names normalizing to one key means the second silently
            # replaces the first — the same collision this rejects, arriving by
            # a quieter route.
            raise ConfigError(
                f"{resolved} defines both {spellings[key]!r} and {name!r}, which "
                f"differ only in punctuation; rename one of them"
            )
        spellings[key] = name
        try:
            profiles[key] = model.model_validate(raw_profile or {})
        except ValidationError as exc:
            raise ConfigError(
                f"profile {name!r} in {resolved} is invalid: {format_validation_error(exc)}"
            ) from exc
    return profiles
