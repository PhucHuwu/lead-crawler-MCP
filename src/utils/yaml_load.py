"""YAML loading with the quiet failure modes made loud.

Two things are true of every configuration file this project reads, and both are
handled here so that each loader does not have to remember them:

* A file that is missing, unreadable, empty, or not a mapping of names to
  settings is a configuration error, not an empty configuration. Failing with an
  empty profile list would turn a typo in ``LEAD_SEARCH_PROFILES_PATH`` into a
  run that silently searches for everyone.
* A duplicated key is refused. PyYAML keeps the last of two identical keys and
  says nothing, so a profile pasted twice loses its first copy without a warning
  — the file loads, the run succeeds, and half of what someone wrote is not in
  effect. Nothing downstream can detect that, so it is caught at parse time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.utils.errors import ConfigError


class _StrictLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses a duplicated mapping key.

    Subclassed rather than patched globally so the strictness applies to this
    project's configuration files and to nothing else in the process.
    """


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    """Build a mapping, rejecting a key that appears twice."""
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in mapping
        except TypeError as exc:  # an unhashable key (a list, say) cannot be a name
            raise yaml.constructor.ConstructorError(
                None, None, f"mapping key {key!r} is not usable as a name", key_node.start_mark
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_yaml_mapping(
    path: str | Path,
    *,
    label: str,
    env_var: str,
) -> dict[Any, Any]:
    """Read ``path`` as a YAML mapping, or fail with a message naming the file.

    Args:
        path: File to read.
        label: What the file holds, for the error messages — ``"search
            profiles"``, say. Used as ``f"{label} file"``.
        env_var: Environment variable that points at this file, so the not-found
            message tells the reader how to point somewhere else.

    Returns:
        The parsed mapping, keys as written in the file. Callers normalize names.

    Raises:
        ConfigError: if the file is missing, unreadable, invalid YAML, empty, or
            does not contain a mapping at the top level. Duplicate keys are
            reported as invalid YAML.
    """
    resolved = Path(path)

    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{label} file not found: {resolved} (set {env_var} or create the file)"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {label} file {resolved}: {exc}") from exc

    try:
        document = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 — SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(f"{resolved} is not valid YAML: {_describe(exc)}") from exc

    if document is None:
        raise ConfigError(f"{resolved} contains no {label}")
    if not isinstance(document, dict):
        raise ConfigError(
            f"{resolved} must be a mapping of names to settings, got {type(document).__name__}"
        )
    return document


def _describe(error: yaml.YAMLError) -> str:
    """One line for a YAML error, keeping the line number PyYAML found.

    The default ``str()`` of a marked error is several lines of context that read
    badly inside a single-line CLI message, but the line number is the part that
    actually locates the problem.
    """
    problem = getattr(error, "problem", None)
    mark = getattr(error, "problem_mark", None)
    message = str(problem or error).strip()
    if mark is not None:
        return f"{message} (line {mark.line + 1}, column {mark.column + 1})"
    return message
