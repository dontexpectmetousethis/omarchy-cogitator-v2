#!/usr/bin/env python3
"""Bind assistant personalities to the Cogitator engagement.

Each supported application is an adapter with the same three jobs: install the
persona asset through the app's sanctioned path, capture the previous value,
and restore it.  Cogitator's control panel toggles adapters per application.

Supported

    hermes     ``hermes config set personalities.grim-dark_40k`` and
               ``display.personality``; nothing is written to config.yaml by hand.
    opencode   An ``instructions`` entry in ``~/.config/opencode/opencode.json``
               pointing at ``~/.config/opencode/personas/grim-dark-40k.md``.
               Instructions are appended to the system prompt, so OpenCode keeps
               its own base coding prompt.  Only plain JSON is edited; a
               commented ``opencode.jsonc`` is reported and left untouched.

Commands

    install              Register the persona asset where it is missing.
    refresh              Re-render and overwrite the persona assets.
    sync engaged         Bind every enabled adapter.
    sync disengaged       Restore every adapter that has saved state.
    engage / disengage    Aliases for the two sync directions.
    uninstall            Restore, then remove only files this tool wrote.
    status [--json]      Availability, current value, toggles and saved state.

State lives in ``$XDG_STATE_HOME/cogitator-v2/personas.json``: one previous
value per adapter plus an install receipt (path -> sha256) so uninstall never
deletes a file the user wrote.  Re-engaging does not overwrite a saved previous
value.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parents[1]
PERSONA_FILE = SOURCE_DIR / "personas" / "grim-dark-40k.md"
CONTRACT_FILE = SOURCE_DIR / "shared" / "voice" / "persona-contract.md"

HERMES_TARGET = "grim-dark_40k"
#: ``~/`` is resolved from the user's home by OpenCode in every workspace;
#: a relative entry would be searched from the workspace down, so it would
#: miss for repositories outside the home directory.
OPENCODE_REFERENCE = "~/.config/opencode/personas/grim-dark-40k.md"

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
CONF_FILE = CONFIG_HOME / "cogitator-v2" / "cogitator.conf"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state")) / "cogitator-v2"
STATE_FILE = STATE_DIR / "personas.json"
LEGACY_STATE_FILE = STATE_DIR / "hermes-personality.json"
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
OPENCODE_DIR = CONFIG_HOME / "opencode"
OPENCODE_INSTRUCTIONS = OPENCODE_DIR / "personas" / "grim-dark-40k.md"


def _home_reference(path: Path) -> str:
    """Use ``~/`` so OpenCode resolves the entry in every workspace.

    A relative entry is searched from the workspace upward and misses for
    repositories outside the home directory; ``~/`` is documented and stable.
    """
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


#: The value written into ``instructions``.
OPENCODE_REFERENCE = _home_reference(OPENCODE_INSTRUCTIONS)

#: Entries written by earlier builds (workspace-relative).  Removed on the
#: next sync so an upgrade does not leave two pointers to the same file.
LEGACY_OPENCODE_REFERENCES = ("personas/grim-dark-40k.md",)

#: Every adapter can be switched off from the control panel; binding is the
#: master switch.  Defaults keep the feature on for installed applications.
TOGGLE_DEFAULTS = {
    "PERSONA_BINDING": True,
    "PERSONA_HERMES": True,
    "PERSONA_OPENCODE": True,
}

#: sha256 of the first shipped prompt (persona prose only, before the shared
#: voice contract existed).  Install refreshes that value but leaves any other
#: content alone, so a hand-edited personality is never clobbered.
LEGACY_PROMPT_SHA256 = os.environ.get(
    "COGITATOR_LEGACY_PROMPT_SHA256",
    "9f7d1d0c545158eac35a78e161305e4b031ad1f42cb97baaa0ea3d8dd943d47e",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_prompt() -> str:
    """Persona prose plus the shared machine-voice contract, as one prompt."""
    parts: list[str] = []
    for path in (PERSONA_FILE, CONTRACT_FILE):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"cogitator: persona source unreadable: {path} ({exc})")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


# --- configuration ---------------------------------------------------------

def read_conf() -> dict[str, str]:
    try:
        raw = CONF_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def toggles() -> dict[str, bool]:
    values = read_conf()
    result: dict[str, bool] = {}
    for key, default in TOGGLE_DEFAULTS.items():
        raw = values.get(key)
        result[key] = default if raw is None else raw.strip().lower() in ("true", "yes", "1", "on")
    return result


# --- state -----------------------------------------------------------------

def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_state() -> dict:
    data = _read_json(STATE_FILE)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 2)
    data.setdefault("targets", {})
    data.setdefault("installed", {})
    if not isinstance(data["targets"], dict):
        data["targets"] = {}
    if not isinstance(data["installed"], dict):
        data["installed"] = {}
    if "hermes" not in data["targets"]:
        legacy = _read_json(LEGACY_STATE_FILE)
        if isinstance(legacy, dict) and isinstance(legacy.get("previousPersonality"), str):
            data["targets"]["hermes"] = {
                "previous": legacy["previousPersonality"],
                "engagedAt": legacy.get("engagedAt") or _now(),
            }
            save_state(data)
            try:
                LEGACY_STATE_FILE.unlink()
            except OSError:
                pass
    return data


def save_state(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, STATE_FILE)


# --- hermes ----------------------------------------------------------------

def hermes_bin() -> str:
    candidates = (
        os.environ.get("HERMES_BIN", ""),
        shutil.which("hermes") or "",
        str(Path.home() / ".local/bin/hermes"),
        str(HERMES_HOME / "hermes-agent" / ".hermes" / "bin" / "hermes"),
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _run_hermes(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    binary = hermes_bin()
    if not binary:
        return None
    env = dict(os.environ)
    env.setdefault("HERMES_HOME", str(HERMES_HOME))
    try:
        return subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=30, env=env, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _json_result(result: subprocess.CompletedProcess[str] | None) -> object | None:
    if result is None or result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def hermes_definition() -> str | None:
    value = _json_result(
        _run_hermes(["config", "get", f"personalities.{HERMES_TARGET}", "--json"])
    )
    return value if isinstance(value, str) else None


def hermes_current() -> str | None:
    value = _json_result(_run_hermes(["config", "get", "display.personality", "--json"]))
    return value.strip() if isinstance(value, str) else None


def _hermes_write(value: str) -> bool:
    """Two attempts: the desktop may hold the config lock for a moment."""
    for attempt in range(2):
        result = _run_hermes(["config", "set", "display.personality", value])
        if result is not None and result.returncode == 0:
            return True
        if attempt == 0:
            time.sleep(0.4)
    return False


class HermesAdapter:
    id = "hermes"
    flag = "PERSONA_HERMES"
    label = "Hermes Desktop"

    def available(self) -> bool:
        return bool(hermes_bin())

    def current(self) -> str | None:
        return hermes_current()

    def install(self, force: bool = False) -> bool:
        prompt = render_prompt()
        current = hermes_definition()
        if current == prompt and not force:
            return True
        if current and not force and _sha256(current) != LEGACY_PROMPT_SHA256:
            return True  # a user edit: leave it alone
        result = _run_hermes(["config", "set", f"personalities.{HERMES_TARGET}", prompt])
        return result is not None and result.returncode == 0

    def engage(self, state: dict) -> bool:
        if not self.install():
            print("cogitator: could not register the Hermes personality.", file=sys.stderr)
            return False
        target = state["targets"].get(self.id)
        if not target:
            previous = self.current()
            if previous is None:
                print(
                    "cogitator: could not read Hermes' current personality; leaving it unchanged.",
                    file=sys.stderr,
                )
                return False
            target = {"previous": previous, "engagedAt": _now()}
            state["targets"][self.id] = target
            save_state(state)
        if not _hermes_write(HERMES_TARGET):
            if target is not None and "previous" in target:
                state["targets"].pop(self.id, None)
                save_state(state)
            return False
        return True

    def disengage(self, state: dict) -> bool:
        target = state["targets"].get(self.id)
        if not target:
            return True
        previous = target.get("previous", "")
        if not isinstance(previous, str):
            previous = ""
        if not _hermes_write(previous):
            return False
        state["targets"].pop(self.id, None)
        save_state(state)
        return True


# --- opencode --------------------------------------------------------------

class OpenCodeAdapter:
    id = "opencode"
    flag = "PERSONA_OPENCODE"
    label = "OpenCode"

    def config_path(self) -> Path | None:
        for name in ("opencode.json", "opencode.jsonc"):
            path = OPENCODE_DIR / name
            if path.exists():
                return path
        return None

    def available(self) -> bool:
        return shutil.which("opencode") is not None and self.config_path() is not None

    def _read_config(self) -> dict:
        path = self.config_path()
        if path is None:
            raise FileNotFoundError("no opencode.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("opencode config is not an object")
        return data

    def _write_config(self, data: dict) -> bool:
        path = self.config_path()
        if path is None:
            return False
        temp = path.with_name(path.name + ".cogitator.tmp")
        temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
        return True

    def current(self) -> str | None:
        try:
            config = self._read_config()
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        entries = config.get("instructions")
        if isinstance(entries, list) and any(
            entry in entries for entry in (OPENCODE_REFERENCE, *LEGACY_OPENCODE_REFERENCES)
        ):
            return OPENCODE_REFERENCE
        return "default"

    def install(self, force: bool = False, state: dict | None = None) -> bool:
        # Accept the caller's state so a receipt written here survives the
        # save that follows engage(); loading a private copy loses it.
        if state is None:
            state = load_state()
        prompt = render_prompt() + "\n"
        receipt = state["installed"].get(str(OPENCODE_INSTRUCTIONS))
        if OPENCODE_INSTRUCTIONS.exists() and not force:
            existing = OPENCODE_INSTRUCTIONS.read_text(encoding="utf-8")
            if existing == prompt:
                state["installed"][str(OPENCODE_INSTRUCTIONS)] = _sha256(prompt)
                save_state(state)
                return True
            if receipt != _sha256(existing):
                print(
                    f"cogitator: {OPENCODE_INSTRUCTIONS} is not ours; leaving it alone.",
                    file=sys.stderr,
                )
                return False
        OPENCODE_INSTRUCTIONS.parent.mkdir(parents=True, exist_ok=True)
        OPENCODE_INSTRUCTIONS.write_text(prompt, encoding="utf-8")
        state["installed"][str(OPENCODE_INSTRUCTIONS)] = _sha256(prompt)
        save_state(state)
        return True

    def engage(self, state: dict) -> bool:
        if not self.install(state=state):
            return False
        try:
            config = self._read_config()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(
                f"cogitator: {self.config_path()} is not plain JSON ({exc}). "
                f"Add {OPENCODE_REFERENCE!r} to its instructions array by hand; "
                "Cogitator will not rewrite a commented config.",
                file=sys.stderr,
            )
            return False
        entries = config.get("instructions")
        if not isinstance(entries, list):
            entries = []
        cleaned = [entry for entry in entries if entry not in LEGACY_OPENCODE_REFERENCES]
        if OPENCODE_REFERENCE not in cleaned:
            cleaned.append(OPENCODE_REFERENCE)
        foreign = [entry for entry in entries if entry not in (OPENCODE_REFERENCE, *LEGACY_OPENCODE_REFERENCES)]
        target = state["targets"].get(self.id)
        created = target is None
        if created:
            # An empty array or a leftover pointer of ours is not user content:
            # disengage should remove the key again, not leave [] behind.
            target = {"hadInstructionsKey": bool(foreign), "engagedAt": _now()}
            state["targets"][self.id] = target
        if cleaned != entries or "instructions" not in config:
            config["instructions"] = cleaned
            if not self._write_config(config):
                if created:
                    state["targets"].pop(self.id, None)
                save_state(state)
                return False
        save_state(state)
        return True

    def disengage(self, state: dict) -> bool:
        target = state["targets"].get(self.id)
        if target is None:
            return True
        try:
            config = self._read_config()
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        entries = config.get("instructions")
        if isinstance(entries, list) and any(
            entry in entries for entry in (OPENCODE_REFERENCE, *LEGACY_OPENCODE_REFERENCES)
        ):
            entries = [
                entry
                for entry in entries
                if entry not in (OPENCODE_REFERENCE, *LEGACY_OPENCODE_REFERENCES)
            ]
            if entries or target.get("hadInstructionsKey"):
                config["instructions"] = entries
            else:
                config.pop("instructions", None)
            if not self._write_config(config):
                return False
        state["targets"].pop(self.id, None)
        self._remove_asset(state)
        save_state(state)
        return True

    def _remove_asset(self, state: dict) -> None:
        receipt = state["installed"].get(str(OPENCODE_INSTRUCTIONS))
        if receipt is None or not OPENCODE_INSTRUCTIONS.exists():
            return
        try:
            if _sha256(OPENCODE_INSTRUCTIONS.read_text(encoding="utf-8")) != receipt:
                return
            OPENCODE_INSTRUCTIONS.unlink()
        except OSError:
            return
        state["installed"].pop(str(OPENCODE_INSTRUCTIONS), None)


ADAPTERS = (HermesAdapter(), OpenCodeAdapter())


# --- commands --------------------------------------------------------------

def install_all(force: bool = False) -> int:
    failures = 0
    for adapter in ADAPTERS:
        if not adapter.available():
            continue
        if not adapter.install(force=force):
            failures += 1
    return 1 if failures else 0


def sync(engaged: bool) -> int:
    settings = toggles()
    binding = settings["PERSONA_BINDING"]
    state = load_state()
    for adapter in ADAPTERS:
        if not adapter.available():
            continue
        desired = engaged and binding and settings[adapter.flag]
        try:
            if desired:
                ok = adapter.engage(state)
            else:
                ok = adapter.disengage(state)
        except Exception as exc:  # an adapter must never block the engagement
            ok = False
            print(f"cogitator: {adapter.label} persona failed: {exc}", file=sys.stderr)
        if not ok:
            print(f"cogitator: {adapter.label} persona was not changed.", file=sys.stderr)
    return 0


def uninstall() -> int:
    state = load_state()
    for adapter in ADAPTERS:
        if adapter.available():
            try:
                adapter.disengage(state)
            except Exception:
                pass
    return 0


def status(as_json: bool = False) -> int:
    settings = toggles()
    state = load_state()
    if as_json:
        payload = {
            "binding": settings["PERSONA_BINDING"],
            "toggles": {adapter.id: settings[adapter.flag] for adapter in ADAPTERS},
            "targets": state["targets"],
            "installed": state["installed"],
            "adapters": [
                {
                    "id": adapter.id,
                    "label": adapter.label,
                    "available": adapter.available(),
                    "enabled": settings[adapter.flag],
                    "current": adapter.current() if adapter.available() else None,
                    "engaged": adapter.id in state["targets"],
                }
                for adapter in ADAPTERS
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0
    print(f"VOICE BINDING: {'engaged' if settings['PERSONA_BINDING'] else 'suspended'}")
    for adapter in ADAPTERS:
        if not adapter.available():
            print(f"  {adapter.label}: not installed")
            continue
        bound = "bound" if adapter.id in state["targets"] else "restored"
        enabled = "enabled" if settings[adapter.flag] else "disabled"
        print(f"  {adapter.label}: {enabled}, {bound}, current={adapter.current()}")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    argument = sys.argv[2] if len(sys.argv) > 2 else ""
    if command == "install":
        return install_all()
    if command == "refresh":
        return install_all(force=True)
    if command == "sync":
        if argument not in ("engaged", "disengaged"):
            print("usage: persona.py sync <engaged|disengaged>", file=sys.stderr)
            return 2
        return sync(argument == "engaged")
    if command == "engage":
        return sync(True)
    if command == "disengage":
        return sync(False)
    if command == "uninstall":
        return uninstall()
    if command == "status":
        return status(as_json="--json" in sys.argv[2:])
    print(
        f"usage: {Path(sys.argv[0]).name} <install|refresh|sync engaged|sync disengaged|uninstall|status>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
