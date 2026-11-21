#!/usr/bin/env python3
"""Tests for the persona bridge; never touch the real Hermes or OpenCode homes."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "persona.py"
WRAPPER = ROOT / "scripts" / "cogitator"
HERMES_TARGET = "grim-dark_40k"

FAKE_HERMES = '''#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_HERMES_CONFIG"]
try:
    data = json.load(open(path))
except FileNotFoundError:
    data = {}
args = sys.argv[1:]
if args[:2] == ["config", "get"]:
    key = args[2]
    if key not in data:
        print("Config key not set: " + key)
        sys.exit(1)
    print(json.dumps(data[key]))
    sys.exit(0)
if args[:2] == ["config", "set"]:
    data[args[2]] = args[3]
    json.dump(data, open(path, "w"))
    sys.exit(0)
if args[:2] == ["config", "unset"]:
    data.pop(args[2], None)
    json.dump(data, open(path, "w"))
    sys.exit(0)
sys.exit(2)
'''


class PersonaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.fake = self.bin / "hermes"
        self.fake.write_text(FAKE_HERMES)
        self.fake.chmod(0o755)
        self.opencode_bin = self.bin / "opencode"
        self.opencode_bin.write_text("#!/bin/sh\nexit 0\n")
        self.opencode_bin.chmod(0o755)
        self.config_home = self.root / "config"
        self.opencode_dir = self.config_home / "opencode"
        self.opencode_dir.mkdir(parents=True)
        self.opencode_config = self.opencode_dir / "opencode.json"
        self.opencode_config.write_text(json.dumps({"$schema": "https://opencode.ai/config.json"}))
        self.hermes_config = self.root / "hermes-config.json"
        self.state_home = self.root / "state"
        self.conf = self.config_home / "cogitator-v2" / "cogitator.conf"
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(self.home),
            "HERMES_BIN": str(self.fake),
            "HERMES_HOME": str(self.root / "hermes"),
            "FAKE_HERMES_CONFIG": str(self.hermes_config),
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_STATE_HOME": str(self.state_home),
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
        })
        self.write_hermes({"display.personality": "tech-first"})

    def tearDown(self):
        self.tmp.cleanup()

    # --- helpers ---------------------------------------------------------

    def write_hermes(self, data):
        self.hermes_config.write_text(json.dumps(data))

    def read_hermes(self):
        return json.loads(self.hermes_config.read_text())

    def write_opencode(self, data):
        self.opencode_config.write_text(json.dumps(data))

    def read_opencode(self):
        return json.loads(self.opencode_config.read_text())

    def write_conf(self, **values):
        self.conf.parent.mkdir(parents=True, exist_ok=True)
        self.conf.write_text("".join(f"{key}={str(value).lower()}\n" for key, value in values.items()))

    def run_helper(self, *args):
        return subprocess.run(
            [sys.executable, str(HELPER), *args],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def state(self):
        path = self.state_home / "cogitator-v2" / "personas.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def persona_file(self):
        return self.opencode_dir / "personas" / "grim-dark-40k.md"

    @property
    def reference(self):
        # XDG_CONFIG_HOME is a temp dir here, so the entry is absolute.
        return str(self.persona_file())

    # --- install ---------------------------------------------------------

    def test_install_registers_both_assets(self):
        result = self.run_helper("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        hermes_prompt = self.read_hermes()[f"personalities.{HERMES_TARGET}"]
        self.assertIn("Adeptus Astartes", hermes_prompt)
        self.assertIn("Machine voice contract", hermes_prompt)
        self.assertIn("LINK ESTABLISHED", hermes_prompt)
        opencode_prompt = self.persona_file().read_text()
        self.assertIn("Adeptus Astartes", opencode_prompt)
        self.assertIn("Machine voice contract", opencode_prompt)

    def test_install_leaves_a_user_edited_hermes_prompt_alone(self):
        self.write_hermes({
            "display.personality": "tech-first",
            f"personalities.{HERMES_TARGET}": "My own grim voice.",
        })
        result = self.run_helper("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_hermes()[f"personalities.{HERMES_TARGET}"], "My own grim voice.")

    def test_refresh_overwrites_a_user_edited_hermes_prompt(self):
        self.write_hermes({
            "display.personality": "tech-first",
            f"personalities.{HERMES_TARGET}": "My own grim voice.",
        })
        result = self.run_helper("refresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Machine voice contract", self.read_hermes()[f"personalities.{HERMES_TARGET}"])

    def test_install_refreshes_the_legacy_shipped_prompt(self):
        legacy = "You are a voice out of the machine-god's silence."
        self.env["COGITATOR_LEGACY_PROMPT_SHA256"] = hashlib.sha256(legacy.encode()).hexdigest()
        self.write_hermes({
            "display.personality": "tech-first",
            f"personalities.{HERMES_TARGET}": legacy,
        })
        result = self.run_helper("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Machine voice contract", self.read_hermes()[f"personalities.{HERMES_TARGET}"])

    def test_foreign_opencode_persona_file_is_not_overwritten(self):
        self.persona_file().parent.mkdir(parents=True)
        self.persona_file().write_text("hand written by the user\n")
        result = self.run_helper("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.persona_file().read_text(), "hand written by the user\n")

    # --- binding ---------------------------------------------------------

    def test_sync_engaged_binds_both_and_captures_previous(self):
        result = self.run_helper("sync", "engaged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_hermes()["display.personality"], HERMES_TARGET)
        self.assertEqual(self.read_opencode()["instructions"], [self.reference])
        targets = self.state()["targets"]
        self.assertEqual(targets["hermes"]["previous"], "tech-first")
        self.assertFalse(targets["opencode"]["hadInstructionsKey"])

    def test_sync_disengaged_restores_both(self):
        self.assertEqual(self.run_helper("sync", "engaged").returncode, 0)
        result = self.run_helper("sync", "disengaged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_hermes()["display.personality"], "tech-first")
        self.assertNotIn("instructions", self.read_opencode())
        self.assertFalse(self.persona_file().exists())
        self.assertEqual(self.state()["targets"], {})

    def test_reengage_keeps_the_original_previous_value(self):
        self.assertEqual(self.run_helper("sync", "engaged").returncode, 0)
        data = self.read_hermes()
        data["display.personality"] = "concise"
        self.write_hermes(data)

        self.assertEqual(self.run_helper("sync", "engaged").returncode, 0)
        self.assertEqual(self.read_hermes()["display.personality"], HERMES_TARGET)
        self.assertEqual(self.state()["targets"]["hermes"]["previous"], "tech-first")

    def test_per_app_toggle_skips_a_disabled_adapter(self):
        self.write_conf(PERSONA_OPENCODE="false")
        result = self.run_helper("sync", "engaged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_hermes()["display.personality"], HERMES_TARGET)
        self.assertNotIn("instructions", self.read_opencode())
        self.assertNotIn("opencode", self.state()["targets"])

    def test_master_toggle_restores_everything(self):
        self.assertEqual(self.run_helper("sync", "engaged").returncode, 0)
        self.write_conf(PERSONA_BINDING="false")
        result = self.run_helper("sync", "engaged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_hermes()["display.personality"], "tech-first")
        self.assertNotIn("instructions", self.read_opencode())
        self.assertEqual(self.state()["targets"], {})

    def test_preexisting_instructions_are_preserved(self):
        self.write_opencode({"instructions": ["AGENTS.md"]})
        self.assertEqual(self.run_helper("sync", "engaged").returncode, 0)
        self.assertEqual(self.read_opencode()["instructions"], ["AGENTS.md", self.reference])
        self.assertEqual(self.run_helper("sync", "disengaged").returncode, 0)
        self.assertEqual(self.read_opencode()["instructions"], ["AGENTS.md"])

    def test_legacy_opencode_entry_is_replaced_and_removed(self):
        self.write_opencode({"instructions": ["AGENTS.md", "personas/grim-dark-40k.md"]})
        self.assertEqual(self.run_helper("sync", "engaged").returncode, 0)
        self.assertEqual(self.read_opencode()["instructions"], ["AGENTS.md", self.reference])
        self.assertEqual(self.run_helper("sync", "disengaged").returncode, 0)
        self.assertEqual(self.read_opencode()["instructions"], ["AGENTS.md"])

    def test_unavailable_opencode_is_skipped(self):
        self.env["PATH"] = f"{self.bin}:/usr/bin:/bin"
        self.opencode_bin.unlink()
        result = self.run_helper("sync", "engaged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_hermes()["display.personality"], HERMES_TARGET)
        self.assertNotIn("instructions", self.read_opencode())

    def test_unreadable_opencode_config_does_not_block_hermes(self):
        self.opencode_config.write_text("{ // comments make this JSONC\n}")
        result = self.run_helper("sync", "engaged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_hermes()["display.personality"], HERMES_TARGET)
        self.assertEqual(self.opencode_config.read_text(), "{ // comments make this JSONC\n}")

    def test_legacy_state_migrates_and_restores(self):
        state_dir = self.state_home / "cogitator-v2"
        state_dir.mkdir(parents=True)
        (state_dir / "hermes-personality.json").write_text(json.dumps({
            "version": 1,
            "previousPersonality": "tech-first",
            "engagedPersonality": HERMES_TARGET,
        }))
        self.write_hermes({
            "display.personality": HERMES_TARGET,
            f"personalities.{HERMES_TARGET}": "seeded",
        })
        result = self.run_helper("sync", "disengaged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_hermes()["display.personality"], "tech-first")
        self.assertFalse((state_dir / "hermes-personality.json").exists())

    # --- status and wiring ----------------------------------------------

    def test_status_reports_both_adapters(self):
        self.assertEqual(self.run_helper("sync", "engaged").returncode, 0)
        result = self.run_helper("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["binding"])
        self.assertEqual({entry["id"] for entry in payload["adapters"]}, {"hermes", "opencode"})
        self.assertTrue(all(entry["available"] for entry in payload["adapters"]))
        self.assertTrue(all(entry["engaged"] for entry in payload["adapters"]))

    def test_wrapper_wires_the_persona_script(self):
        wrapper = WRAPPER.read_text()
        self.assertIn('PERSONA_SCRIPT="$SOURCE_DIR/scripts/persona.py"', wrapper)
        self.assertIn('"$PERSONA_SCRIPT" install', wrapper)
        self.assertIn('"$PERSONA_SCRIPT" sync engaged', wrapper)
        self.assertIn('"$PERSONA_SCRIPT" sync disengaged', wrapper)
        self.assertIn('"$PERSONA_SCRIPT" uninstall', wrapper)


class VoiceContractTests(unittest.TestCase):
    """The persona and the interface must share one vocabulary."""

    def test_canonical_tokens_exist_in_the_ui_vocabulary(self):
        lexicon = json.loads((ROOT / "shared" / "voice" / "lexicon.json").read_text())
        phrases = json.loads((ROOT / "shared" / "voice" / "phrases.json").read_text())
        values = set(lexicon.values()) | set(phrases.values())
        contract = (ROOT / "shared" / "voice" / "persona-contract.md").read_text()
        block = contract.split("## Canonical machine-state tokens", 1)[1]
        tokens = [line.strip() for line in block.splitlines() if line.strip()]
        self.assertTrue(tokens)
        missing = [token for token in tokens if token not in values]
        self.assertEqual(missing, [], f"tokens absent from shared/voice: {missing}")

    def test_persona_carries_no_unicode_emoji(self):
        persona = (ROOT / "personas" / "grim-dark-40k.md").read_text()
        emoji = [char for char in persona if "\U0001F000" <= char <= "\U0001FAFF"]
        self.assertEqual(emoji, [])


if __name__ == "__main__":
    unittest.main()
