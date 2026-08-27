"""Conformance tests for the shipped agent plugin (`plugin/`).

The plugin is the agent-facing half of this repo: a skill plus reference docs
that describe this codebase's own tool contract. Because it is prose, nothing
in the normal test suite would notice it drifting away from the code it
documents. These tests are that noticing.

The path test is the load-bearing one. Before the plugin existed, the operator
skill lived outside the repo and its workflows invoked `main.py status` from
the repo root long after the entrypoint had moved to `src/main.py`. Both
commands failed and no test in either tree caught it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugin"
SKILL_DIR = PLUGIN / "skills" / "rsa-operator"

MANIFESTS = [
    PLUGIN / "plugin.json",
    PLUGIN / "mcp.json",
    PLUGIN / ".claude-plugin" / "plugin.json",
    PLUGIN / ".mcp.json",
    REPO_ROOT / ".claude-plugin" / "marketplace.json",
]

# Path-shaped tokens the plugin docs may reference in this repo.
_REPO_PATH_RE = re.compile(r"(?<![\w/.$])((?:src|specs|scripts|tests|docs)/[\w./-]+)")
_SKILL_PATH_RE = re.compile(r"(?<![\w/.$])(references/[\w./-]+\.md)")
# Any script the docs tell the agent to run. Catches a path that lost its
# `src/` prefix, which is the exact drift that went unnoticed before.
_SCRIPT_RE = re.compile(r"(?<![\w/.$-])([\w./-]+\.(?:py|sh))(?![\w/])")


def _plugin_docs() -> list[Path]:
    return sorted(SKILL_DIR.rglob("*.md"))


def _read_frontmatter(path: Path) -> dict[str, str]:
    """Minimal frontmatter reader.

    Deliberately not PyYAML: the plugin must stay installable without adding a
    test-only dependency to a trading app. Handles `key: value`, folded blocks
    (`key: >-`), and skips nested maps, which is every shape the skill uses.
    """
    text = path.read_text()
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    body = text.split("---\n", 2)[1]
    out: dict[str, str] = {}
    key: str | None = None
    for line in body.splitlines():
        if not line.strip():
            continue
        if line.startswith(("  ", "\t")):
            if key:
                out[key] = (out[key] + " " + line.strip()).strip()
            continue
        m = re.match(r"^([\w-]+):\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        out[key] = "" if value in {">", ">-", "|", "|-"} else value
    return out


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: str(p.name))
def test_manifest_is_valid_json(manifest: Path):
    assert manifest.exists(), f"missing manifest: {manifest}"
    json.loads(manifest.read_text())


def test_portable_plugin_manifest_required_fields():
    data = json.loads((PLUGIN / "plugin.json").read_text())
    assert data["$schema"].startswith("https://agent-plugins.org/schemas/")
    name = data["name"]
    assert 1 <= len(name) <= 64
    assert re.fullmatch(r"[a-z0-9]+([.-][a-z0-9]+)*", name), name


def test_portable_and_client_manifests_agree():
    portable = json.loads((PLUGIN / "plugin.json").read_text())
    client = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    for key in ("name", "version", "description"):
        assert portable[key] == client[key], f"{key} diverged between manifests"


def test_mcp_manifests_declare_the_router_with_an_explicit_cwd():
    for path, root_var in (
        (PLUGIN / "mcp.json", "PLUGIN_ROOT"),
        (PLUGIN / ".mcp.json", "CLAUDE_PLUGIN_ROOT"),
    ):
        server = json.loads(path.read_text())["mcpServers"]["ssg-router"]
        assert server["type"] == "stdio"
        assert server["args"] == ["-m", "agentic.router"]
        # An explicit cwd is what replaced the `bash -lc "cd ... && exec"`
        # wrapper. Without it the store path resolves against whatever
        # directory the client happened to launch from.
        assert root_var in server["cwd"], f"{path} lost its cwd anchor"
        assert "SSG_DB_PATH" in server["env"], f"{path} must pass SSG_DB_PATH through"
        assert "bash" not in server["command"]


def test_marketplace_points_at_the_plugin_directory():
    data = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entry = next(p for p in data["plugins"] if p["name"] == "stockshotgun-rsa")
    assert (REPO_ROOT / entry["source"].lstrip("./")).is_dir()


def test_skill_frontmatter_conforms_to_agent_skills_spec():
    fm = _read_frontmatter(SKILL_DIR / "SKILL.md")
    name = fm["name"]
    assert name == SKILL_DIR.name, "name must match the skill directory"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), name
    assert 1 <= len(name) <= 64
    assert 1 <= len(fm["description"]) <= 1024
    assert len(fm.get("compatibility", "")) <= 500


def test_skill_carries_no_personal_or_host_specific_content():
    """The repo is public. Operator identity, LifeOS paths, and the Pulse
    notification endpoint belong to the private shim, not here."""
    forbidden = ("Taylor", "/home/", "LIFEOS", "SKILLCUSTOMIZATIONS", "localhost:31337")
    for doc in _plugin_docs():
        text = doc.read_text()
        for token in forbidden:
            assert token not in text, f"{doc.relative_to(REPO_ROOT)} leaks {token!r}"


@pytest.mark.parametrize("doc", _plugin_docs(), ids=lambda p: p.name)
def test_repo_paths_named_in_plugin_docs_exist(doc: Path):
    missing = [
        token
        for token in sorted(set(_REPO_PATH_RE.findall(doc.read_text())))
        if not (REPO_ROOT / token).exists()
    ]
    assert not missing, f"{doc.relative_to(REPO_ROOT)} names nonexistent paths: {missing}"


@pytest.mark.parametrize("doc", _plugin_docs(), ids=lambda p: p.name)
def test_skill_relative_references_exist(doc: Path):
    missing = [
        token
        for token in sorted(set(_SKILL_PATH_RE.findall(doc.read_text())))
        if not (SKILL_DIR / token).exists()
    ]
    assert not missing, f"{doc.relative_to(REPO_ROOT)} names missing references: {missing}"


@pytest.mark.parametrize("doc", _plugin_docs(), ids=lambda p: p.name)
def test_scripts_named_in_plugin_docs_are_runnable_paths(doc: Path):
    """A script named without its `src/` prefix resolves to nothing from the
    repo root. That is precisely how `main.py status` survived in the operator
    workflows after the entrypoint moved to `src/main.py`."""
    missing = [
        token
        for token in sorted(set(_SCRIPT_RE.findall(doc.read_text())))
        if not (REPO_ROOT / token).exists() and not (SKILL_DIR / token).exists()
    ]
    assert not missing, (
        f"{doc.relative_to(REPO_ROOT)} names scripts that do not exist "
        f"relative to the repo root or the skill directory: {missing}"
    )
