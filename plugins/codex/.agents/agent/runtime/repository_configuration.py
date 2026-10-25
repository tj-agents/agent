import json
import math
import re
import stat
from pathlib import Path


SETTINGS_PATH = Path(".agents") / "settings.json"
NAMESPACE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
CORE_KEYS = frozenset({"auto_merge", "auto_approve", "permissions"})
AUTO_APPROVE_KEYS = frozenset({"all", "design"})
PERMISSION_MODES = frozenset({"bypass", "standard"})


class ConfigurationViolation(ValueError):
    pass


def _redirected(path):
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ConfigurationViolation(f"duplicate settings key {key!r}")
        value[key] = item
    return value


def _constant(value):
    raise ConfigurationViolation(f"settings contain invalid JSON constant {value!r}")


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ConfigurationViolation("settings contain a non-finite number")
    return parsed


def load(root):
    root = Path(root)
    if not root.is_absolute():
        raise ConfigurationViolation("settings root must be absolute")
    agents = root / ".agents"
    try:
        agents.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigurationViolation(f"settings directory could not be inspected: {error}") from error
    if _redirected(agents) or not agents.is_dir():
        raise ConfigurationViolation("settings directory must not be redirected")
    path = root / SETTINGS_PATH
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigurationViolation(f"settings could not be inspected: {error}") from error
    if _redirected(path) or not path.is_file():
        raise ConfigurationViolation("settings must be a regular file")
    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_float=_float,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ConfigurationViolation) as error:
        raise ConfigurationViolation(f"settings could not be read: {error}") from error
    if not isinstance(parsed, dict):
        raise ConfigurationViolation("settings must be an object")
    configuration = parsed.get("configuration", {})
    if not isinstance(configuration, dict):
        raise ConfigurationViolation("settings configuration must be an object")
    for namespace, value in configuration.items():
        if not isinstance(namespace, str) or not NAMESPACE_NAME.fullmatch(namespace):
            raise ConfigurationViolation(f"settings namespace {namespace!r} is invalid")
        if not isinstance(value, dict):
            raise ConfigurationViolation(f"settings namespace {namespace!r} must be an object")
    return configuration


def core(root):
    configuration = load(root)
    if configuration is None:
        return None
    value = configuration.get("core", {})
    unknown = set(value) - CORE_KEYS
    if unknown:
        raise ConfigurationViolation(f"core settings have unknown keys {sorted(unknown)!r}")
    if "auto_merge" in value and type(value["auto_merge"]) is not bool:
        raise ConfigurationViolation("core auto_merge must be a boolean")
    if "permissions" in value and (not isinstance(value["permissions"], str)
                                    or value["permissions"] not in PERMISSION_MODES):
        raise ConfigurationViolation("core permissions must be bypass or standard")
    if "auto_approve" in value:
        auto_approve = value["auto_approve"]
        if not isinstance(auto_approve, dict):
            raise ConfigurationViolation("core auto_approve must be an object")
        unknown_auto_approve = set(auto_approve) - AUTO_APPROVE_KEYS
        if unknown_auto_approve:
            raise ConfigurationViolation(
                f"core auto_approve has unknown keys {sorted(unknown_auto_approve)!r}"
            )
        for key in AUTO_APPROVE_KEYS:
            if key in auto_approve and type(auto_approve[key]) is not bool:
                raise ConfigurationViolation(f"core auto_approve {key} must be a boolean")
    return value


def auto_merge(root):
    settings = core(root)
    if settings is None:
        return False
    return settings.get("auto_merge", False)


def permissions(root):
    settings = core(root)
    return "bypass" if settings is None else settings.get("permissions", "bypass")


def auto_approve_design(root):
    settings = core(root)
    if settings is None:
        return False
    auto_approve = settings.get("auto_approve")
    if auto_approve is None:
        return False
    if "design" in auto_approve:
        return auto_approve["design"]
    return auto_approve.get("all", False)


def auto_approve_design_provenance(root):
    settings = core(root)
    path = Path(root) / SETTINGS_PATH
    if settings is None:
        return False, f"no settings file at {path}"
    auto_approve = settings.get("auto_approve")
    if auto_approve is not None:
        if "design" in auto_approve:
            return auto_approve["design"], f"configuration.core.auto_approve.design at {path}"
        if "all" in auto_approve:
            return auto_approve["all"], f"configuration.core.auto_approve.all at {path}"
        return False, f"configuration.core.auto_approve default at {path}"
    return False, f"configuration.core.auto_approve default at {path}"
