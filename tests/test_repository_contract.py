import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("itchio-assets", "itchio-publish")


def test_repository_has_two_independent_skill_roots():
    for skill_name in SKILLS:
        root = ROOT / "skill" / skill_name
        assert (root / "SKILL.md").is_file()
        assert (root / "tools.yaml").is_file()


def test_tools_have_explicit_execution_and_annotations():
    for skill_name in SKILLS:
        document = yaml.safe_load((ROOT / "skill" / skill_name / "tools.yaml").read_text("utf-8"))
        assert document["tools"]
        for tool in document["tools"]:
            assert tool["execution"] in {"sync", "async"}
            assert tool["affinity"] == "any"
            assert tool["timeout_hint_secs"] > 0
            assert tool["enforce_thread_affinity"] is True
            assert {
                "read_only_hint",
                "destructive_hint",
                "idempotent_hint",
                "open_world_hint",
            } <= set(tool["annotations"])
            assert tool["input_schema"]["type"] == "object"
            assert tool["output_schema"]["type"] == "object"


def test_production_scripts_do_not_mutate_python_path_or_embed_secrets():
    forbidden = (
        "sys.path.insert",
        "sys.path.append",
        "BUTLER_API_KEY=",
        "ITCHIO_API_KEY=",
    )
    key_pattern = re.compile(r"(?:api[_-]?key|token)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_-]{24,}", re.I)
    for script in (ROOT / "skill").glob("*/scripts/*.py"):
        text = script.read_text("utf-8")
        for marker in forbidden:
            assert marker not in text, "{} contains {}".format(script, marker)
        assert not key_pattern.search(text), "{} looks like it embeds a credential".format(script)
