"""Packet 5 slice C — Hermes skills become discoverable to Claude Code.

The reporter's finding: nothing Hermes owns is visible to the adapter's session.
The adapter child runs with ``HOME=<profile home>``
(``hermes_constants.apply_subprocess_home_env`` via ``hermes_subprocess_env``),
that home has no ``.claude/skills`` directory, and Hermes' own layout is
``<root>/<category>/<skill>/SKILL.md`` — two levels, where Claude Code's
personal-skill discovery is one. So the bridge has to *flatten*, and flattening
across three roots holding 256 skills with 20+ duplicate basenames cannot be
collision-free by construction. That is why this is an explicit allow-list and
not a "link everything" sweep.

The cautionary tale is in the same tree: eight symlinks under the reporter's
real ``~/.claude/skills`` still point at a flat layout abandoned in a 2026-07-27
reorg. They broke because they were created once and never regenerated, and they
sit in a directory a human also edits by hand. Both facts are load-bearing here —
the bridge regenerates from config on every connect, and it will only ever
replace or remove a name its own manifest claims.

What each class pins:

``TestBridgeConfig``      the key is off by default, parses the selector
                          grammar, and every rejection narrows
``TestLinkDir``           the link dir is the ADAPTER CHILD's config dir, never
                          ``Path.home()`` — two ``HERMES_HOME``s get two dirs
``TestDiscovery``         depth, the ``SKILL.md`` requirement, ``skills.disabled``
``TestPlan``              the collision matrix, exclude-after-include, the cap,
                          and the containment guard on link targets
``TestMaterialize``       manifest-owned replacement and removal; an unmanaged
                          entry is never touched; broken managed links self-heal
``TestFailOpen``          a materialization failure costs a WARNING, never a turn
``TestToolVocabulary``    a bridged skill whose prose names Hermes-native tools
                          the adapter does not have is reported, never rewritten
``TestSessionOrdering``   the E2E: a REAL fake-adapter subprocess dumps its own
                          ``$HOME/.claude/skills`` listing AT ``session/new``, so
                          the assertion is about ordering, not merely about files
                          existing somewhere

``HOME`` is deliberately set per test: ``tests/conftest.py`` isolates
``HERMES_HOME`` but explicitly does NOT isolate ``HOME``, and this is the one
feature in the runtime whose output path is derived from it. Nothing here may
write to the developer's real ``~/.claude``.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest
import yaml

import run_agent
from agent import claude_acp_skills as bridge
from tests.agent import _fake_claude_acp_adapter as fake  # noqa: F401

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")

SKILL_MD = """---
name: {name}
description: {description}
---

# {title}

{body}
"""


# --------------------------------------------------------------------- helpers


def _skill(root, category, name, *, description="Does a thing.", body="Nothing."):
    """Write ``<root>/<category>/<name>/SKILL.md`` and return its directory."""
    directory = root / category / name if category else root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        SKILL_MD.format(
            name=name, description=description, title=name.title(), body=body
        ),
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def child_home(tmp_path, monkeypatch):
    """A temp ``HOME`` for the adapter child, so no test can reach the real one."""
    home = tmp_path / "childhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("TERMINAL_HOME_MODE", raising=False)
    return home


@pytest.fixture
def write_config():
    """Write ``model.claude_code_acp.skills`` into the real config.yaml.

    Real file, real loader: the bridge is a user-facing config key with no
    env-var route (AGENTS.md §Adding Configuration), and ``HERMES_HOME`` is
    already a per-test tempdir.
    """

    def _write(skills_config, **top_level):
        from hermes_cli.config import get_config_path

        path = get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"model": {"claude_code_acp": {"skills": skills_config}}}
        payload.update(top_level)
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        try:
            from agent.skill_utils import _external_dirs_cache_clear

            _external_dirs_cache_clear()
        except Exception:
            pass
        return path

    return _write


def _plan_names(plan):
    return sorted(link.name for link in plan.links)


def _by_name(plan):
    return {link.name: link for link in plan.links}


# ------------------------------------------------------------------ the config


class TestBridgeConfig:
    """One key, off by default, and every rejection narrows."""

    def test_bridge_is_off_when_nothing_is_configured(self, write_config):
        write_config({})
        cfg = bridge.resolve_bridge_config()
        assert cfg.mode == bridge.BRIDGE_OFF
        assert cfg.enabled is False

    def test_bridge_is_off_when_the_whole_key_is_absent(self):
        cfg = bridge.resolve_bridge_config()
        assert cfg.mode == bridge.BRIDGE_OFF

    def test_link_mode_is_the_opt_in(self, write_config):
        write_config({"bridge": "link", "include": ["a/b"]})
        cfg = bridge.resolve_bridge_config()
        assert cfg.mode == bridge.BRIDGE_LINK
        assert cfg.enabled is True

    def test_an_unknown_mode_falls_back_to_off(self, write_config, caplog):
        write_config({"bridge": "copy", "include": ["a/b"]})
        with caplog.at_level("WARNING"):
            cfg = bridge.resolve_bridge_config()
        assert cfg.mode == bridge.BRIDGE_OFF
        assert any("copy" in record.getMessage() for record in caplog.records)

    def test_selectors_are_parsed_in_config_order(self, write_config):
        write_config({
            "bridge": "link",
            "include": ["software-development/code-review", "grilling",
                        "productivity/*"],
        })
        cfg = bridge.resolve_bridge_config()
        assert [s.raw for s in cfg.include] == [
            "software-development/code-review", "grilling", "productivity/*",
        ]

    def test_a_wildcard_that_is_not_a_single_category_is_rejected(
        self, write_config, caplog
    ):
        write_config({
            "bridge": "link",
            "include": ["*", "**", "**/tdd", "a/**", "re:.*", "ok/*"],
        })
        with caplog.at_level("WARNING"):
            cfg = bridge.resolve_bridge_config()
        assert [s.raw for s in cfg.include] == ["ok/*"]

    def test_max_links_is_clamped_to_the_hard_ceiling(self, write_config):
        write_config({"bridge": "link", "include": ["a/b"], "max_links": 4000})
        assert bridge.resolve_bridge_config().max_links == bridge.MAX_LINKS_CEILING

    def test_a_nonsense_max_links_uses_the_default(self, write_config):
        write_config({"bridge": "link", "include": ["a/b"], "max_links": "lots"})
        assert bridge.resolve_bridge_config().max_links == bridge.DEFAULT_MAX_LINKS

    def test_max_links_zero_is_honoured_as_a_floor_of_zero(self, write_config):
        write_config({"bridge": "link", "include": ["a/b"], "max_links": 0})
        assert bridge.resolve_bridge_config().max_links == 0

    def test_prune_unmanaged_defaults_false(self, write_config):
        write_config({"bridge": "link", "include": ["a/b"]})
        assert bridge.resolve_bridge_config().prune_unmanaged is False

    def test_an_unreadable_config_resolves_to_off(self, write_config, monkeypatch):
        write_config({"bridge": "link", "include": ["a/b"]})

        def _boom():
            raise OSError("config is gone")

        monkeypatch.setattr("hermes_cli.config.load_config", _boom)
        assert bridge.resolve_bridge_config().mode == bridge.BRIDGE_OFF

    def test_configured_roots_replace_the_resolved_hermes_roots(
        self, write_config, tmp_path
    ):
        elsewhere = tmp_path / "elsewhere"
        _skill(elsewhere, "cat", "one")
        write_config({"bridge": "link", "include": ["one"],
                      "roots": [str(elsewhere)]})
        cfg = bridge.resolve_bridge_config()
        assert cfg.roots == [elsewhere.resolve()]

    def test_a_root_that_is_not_a_directory_is_dropped(
        self, write_config, tmp_path, caplog
    ):
        missing = tmp_path / "not-there"
        write_config({"bridge": "link", "include": ["one"],
                      "roots": [str(missing)]})
        with caplog.at_level("WARNING"):
            cfg = bridge.resolve_bridge_config()
        assert cfg.roots == []

    def test_default_roots_are_hermes_own_resolved_roots(self, write_config):
        from hermes_constants import get_skills_dir

        write_config({"bridge": "link", "include": ["one"]})
        cfg = bridge.resolve_bridge_config()
        assert get_skills_dir().resolve() in cfg.roots


# ------------------------------------------------------------------ where to


class TestLinkDir:
    """The dir is derived from the CHILD's environment, never ``Path.home()``."""

    def test_it_is_the_adapter_childs_home_not_the_process_home(
        self, tmp_path, monkeypatch, child_home
    ):
        assert bridge.default_link_dir() == child_home / ".claude" / "skills"

    def test_profile_home_mode_puts_it_under_hermes_home(
        self, tmp_path, monkeypatch, child_home
    ):
        hermes_home = tmp_path / "hermes_test"
        profile_home = hermes_home / "home"
        profile_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("TERMINAL_HOME_MODE", "profile")
        assert bridge.default_link_dir() == profile_home / ".claude" / "skills"

    def test_two_hermes_homes_get_two_different_dirs(
        self, tmp_path, monkeypatch, child_home
    ):
        monkeypatch.setenv("TERMINAL_HOME_MODE", "profile")
        first = tmp_path / "home-a"
        (first / "home").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(first))
        a = bridge.default_link_dir()
        second = tmp_path / "home-b"
        (second / "home").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(second))
        b = bridge.default_link_dir()
        assert a != b
        assert a == first / "home" / ".claude" / "skills"
        assert b == second / "home" / ".claude" / "skills"

    def test_it_is_never_the_real_user_claude_dir_under_a_temp_home(
        self, tmp_path, monkeypatch, child_home
    ):
        from pathlib import Path as _Path

        resolved = bridge.default_link_dir()
        assert _Path.home() not in resolved.parents or resolved.is_relative_to(
            child_home
        )
        assert resolved.is_relative_to(child_home)

    def test_claude_config_dir_wins_over_home(self, tmp_path, monkeypatch, child_home):
        # The pinned SDK resolves its config dir as
        # `process.env.CLAUDE_CONFIG_DIR ?? homedir()/.claude`, so honouring it
        # is what keeps Hermes writing where the child will actually look.
        elsewhere = tmp_path / "cfgdir"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))
        assert bridge.default_link_dir() == elsewhere / "skills"

    def test_an_explicit_link_dir_override_wins(self, write_config, tmp_path,
                                                child_home):
        override = tmp_path / "explicit"
        write_config({"bridge": "link", "include": ["a"],
                      "link_dir": str(override)})
        cfg = bridge.resolve_bridge_config()
        assert cfg.link_dir == override.resolve()

    def test_a_relative_link_dir_override_is_refused(self, write_config, caplog,
                                                     child_home):
        write_config({"bridge": "link", "include": ["a"], "link_dir": "skills"})
        with caplog.at_level("WARNING"):
            cfg = bridge.resolve_bridge_config()
        assert cfg.link_dir == bridge.default_link_dir()


# ------------------------------------------------------------------- discovery


class TestDiscovery:
    """What counts as a skill, and what is refused before anything is linked."""

    def test_two_level_and_one_level_layouts_are_both_found(self, tmp_path):
        root = tmp_path / "root"
        _skill(root, "software-development", "tdd")
        _skill(root, "", "loose")
        found = bridge.discover_skills([root])
        assert {(s.category, s.name) for s in found} == {
            ("software-development", "tdd"), ("", "loose"),
        }

    def test_a_directory_without_skill_md_is_not_a_skill(self, tmp_path):
        root = tmp_path / "root"
        (root / "cat" / "empty").mkdir(parents=True)
        _skill(root, "cat", "real")
        assert [s.name for s in bridge.discover_skills([root])] == ["real"]

    def test_depth_three_is_not_scanned(self, tmp_path):
        root = tmp_path / "root"
        _skill(root / "cat" / "sub", "deeper", "buried")
        assert bridge.discover_skills([root]) == []

    def test_disabled_skills_are_dropped(self, tmp_path, write_config):
        root = tmp_path / "root"
        _skill(root, "cat", "kept")
        _skill(root, "cat", "banned")
        write_config({}, skills={"disabled": ["banned"]})
        assert [s.name for s in bridge.discover_skills([root])] == ["kept"]

    def test_an_unreadable_skill_md_is_dropped(self, tmp_path):
        root = tmp_path / "root"
        directory = _skill(root, "cat", "locked")
        (directory / "SKILL.md").chmod(0)
        try:
            found = bridge.discover_skills([root])
        finally:
            (directory / "SKILL.md").chmod(stat.S_IRUSR | stat.S_IWUSR)
        if os.geteuid() == 0:  # root reads anything; the branch is still covered
            pytest.skip("running as root: an unreadable file is not unreadable")
        assert found == []

    def test_a_missing_root_is_not_an_error(self, tmp_path):
        assert bridge.discover_skills([tmp_path / "nope"]) == []

    def test_discovery_is_ordered_by_root_then_category_then_name(self, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        _skill(second, "z", "zz")
        _skill(first, "b", "two")
        _skill(first, "a", "one")
        found = bridge.discover_skills([first, second])
        assert [(s.category, s.name) for s in found] == [
            ("a", "one"), ("b", "two"), ("z", "zz"),
        ]


# ------------------------------------------------------------------- the plan


class TestPlan:
    """The collision matrix, the cap, and what a selector may reach."""

    def _cfg(self, roots, include, **kw):
        return bridge.BridgeConfig(
            mode=bridge.BRIDGE_LINK,
            roots=[r for r in roots],
            include=bridge.parse_selectors(include),
            exclude=bridge.parse_selectors(kw.pop("exclude", [])),
            max_links=kw.pop("max_links", bridge.DEFAULT_MAX_LINKS),
            prune_unmanaged=kw.pop("prune_unmanaged", False),
            link_dir=kw.pop("link_dir", None) or (roots[0] / ".links"),
        )

    def test_an_exact_selector_links_exactly_one_skill(self, tmp_path):
        root = tmp_path / "root"
        _skill(root, "software-development", "tdd")
        _skill(root, "software-development", "implement")
        plan = bridge.plan_skill_links(
            self._cfg([root], ["software-development/tdd"])
        )
        assert _plan_names(plan) == ["tdd"]

    def test_a_bare_name_selector_matches_any_category(self, tmp_path):
        root = tmp_path / "root"
        _skill(root, "productivity", "grilling")
        plan = bridge.plan_skill_links(self._cfg([root], ["grilling"]))
        assert _plan_names(plan) == ["grilling"]

    def test_a_category_glob_takes_that_category_only(self, tmp_path):
        root = tmp_path / "root"
        _skill(root, "productivity", "grilling")
        _skill(root, "productivity", "teach")
        _skill(root, "research", "web-extractor")
        plan = bridge.plan_skill_links(self._cfg([root], ["productivity/*"]))
        assert _plan_names(plan) == ["grilling", "teach"]

    def test_exclude_is_applied_after_include(self, tmp_path):
        root = tmp_path / "root"
        _skill(root, "productivity", "grilling")
        _skill(root, "productivity", "teach")
        plan = bridge.plan_skill_links(
            self._cfg([root], ["productivity/*"], exclude=["teach"])
        )
        assert _plan_names(plan) == ["grilling"]

    def test_an_empty_include_links_nothing_and_says_so(self, tmp_path, caplog):
        root = tmp_path / "root"
        _skill(root, "productivity", "grilling")
        with caplog.at_level("WARNING"):
            plan = bridge.plan_skill_links(self._cfg([root], []))
        assert plan.links == []
        assert any("include" in record.getMessage() for record in caplog.records)

    def test_a_selector_that_matches_nothing_is_reported(self, tmp_path, caplog):
        root = tmp_path / "root"
        _skill(root, "productivity", "grilling")
        with caplog.at_level("WARNING"):
            plan = bridge.plan_skill_links(self._cfg([root], ["nope", "grilling"]))
        assert plan.unmatched_selectors == ["nope"]
        assert any("nope" in record.getMessage() for record in caplog.records)

    def test_everything_the_plan_declined_reaches_the_result(self, tmp_path):
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        _skill(outside, "cat", "escapee")
        for index in range(3):
            _skill(root, "cat", f"skill-{index}")
        (root / "cat" / "escapee").symlink_to(outside / "cat" / "escapee")
        cfg = self._cfg(
            [root], ["cat/*", "ghost"], max_links=2, link_dir=tmp_path / "links"
        )
        plan = bridge.plan_skill_links(cfg)
        result = bridge.materialize_skill_links(cfg.link_dir, plan)
        assert result.dropped_over_cap == plan.dropped_over_cap != []
        assert result.unmatched_selectors == ["ghost"]
        assert result.refused_outside_roots == ["escapee"]

    def test_a_basename_collision_across_two_roots_gets_two_distinct_names(
        self, tmp_path
    ):
        a, b = tmp_path / "a", tmp_path / "b"
        _skill(a, "software-development", "code-review")
        _skill(b, "quality", "code-review")
        plan = bridge.plan_skill_links(self._cfg([a, b], ["code-review"]))
        assert len(plan.links) == 2
        assert len(set(_plan_names(plan))) == 2
        assert _plan_names(plan) == ["quality-code-review",
                                     "software-development-code-review"]

    def test_a_collision_that_survives_the_category_gets_the_root_slug(
        self, tmp_path
    ):
        a, b = tmp_path / "alpha", tmp_path / "beta"
        _skill(a, "cat", "dup")
        _skill(b, "cat", "dup")
        plan = bridge.plan_skill_links(self._cfg([a, b], ["dup"]))
        assert len(set(_plan_names(plan))) == 2
        assert all(name.endswith("cat-dup") for name in _plan_names(plan))
        assert {"alpha-cat-dup", "beta-cat-dup"} == set(_plan_names(plan))

    def test_naming_does_not_depend_on_root_order(self, tmp_path):
        a, b = tmp_path / "alpha", tmp_path / "beta"
        _skill(a, "cat", "dup")
        _skill(b, "other", "dup")
        forward = bridge.plan_skill_links(self._cfg([a, b], ["dup"]))
        backward = bridge.plan_skill_links(self._cfg([b, a], ["dup"]))
        assert _plan_names(forward) == _plan_names(backward)

    def test_a_unique_name_stays_bare(self, tmp_path):
        root = tmp_path / "root"
        _skill(root, "software-development", "tdd")
        plan = bridge.plan_skill_links(self._cfg([root], ["tdd"]))
        assert _plan_names(plan) == ["tdd"]

    def test_the_cap_drops_the_overflow_and_reports_it(self, tmp_path, caplog):
        root = tmp_path / "root"
        for index in range(6):
            _skill(root, "cat", f"skill-{index}")
        with caplog.at_level("WARNING"):
            plan = bridge.plan_skill_links(
                self._cfg([root], ["cat/*"], max_links=4)
            )
        assert len(plan.links) == 4
        assert plan.dropped_over_cap == ["skill-4", "skill-5"]
        assert any("4" in record.getMessage() for record in caplog.records)

    def test_the_cap_keeps_a_deterministic_prefix(self, tmp_path):
        root = tmp_path / "root"
        for index in range(6):
            _skill(root, "cat", f"skill-{index}")
        cfg = self._cfg([root], ["cat/*"], max_links=3)
        assert _plan_names(bridge.plan_skill_links(cfg)) == _plan_names(
            bridge.plan_skill_links(cfg)
        )

    def test_max_links_zero_plans_nothing(self, tmp_path):
        root = tmp_path / "root"
        _skill(root, "cat", "one")
        plan = bridge.plan_skill_links(self._cfg([root], ["cat/*"], max_links=0))
        assert plan.links == []

    def test_a_target_outside_every_configured_root_is_refused(
        self, tmp_path, caplog
    ):
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        _skill(outside, "cat", "escapee")
        (root / "cat").mkdir(parents=True)
        (root / "cat" / "escapee").symlink_to(outside / "cat" / "escapee")
        with caplog.at_level("WARNING"):
            plan = bridge.plan_skill_links(self._cfg([root], ["escapee"]))
        assert plan.links == []
        assert plan.refused_outside_roots == ["escapee"]

    def test_the_same_skill_reached_by_two_selectors_is_linked_once(self, tmp_path):
        root = tmp_path / "root"
        _skill(root, "cat", "one")
        plan = bridge.plan_skill_links(self._cfg([root], ["cat/*", "one"]))
        assert _plan_names(plan) == ["one"]

    def test_every_planned_link_is_within_the_cap(self, tmp_path):
        root = tmp_path / "root"
        for index in range(80):
            _skill(root, "cat", f"skill-{index:02d}")
        cfg = self._cfg([root], ["cat/*"])
        plan = bridge.plan_skill_links(cfg)
        assert len(plan.links) <= cfg.max_links


# -------------------------------------------------------------- materializing


class TestMaterialize:
    """Only names the manifest claims are ever replaced or removed."""

    def _plan_of(self, tmp_path, names=("one", "two")):
        root = tmp_path / "root"
        for name in names:
            _skill(root, "cat", name)
        cfg = bridge.BridgeConfig(
            mode=bridge.BRIDGE_LINK,
            roots=[root],
            include=bridge.parse_selectors(["cat/*"]),
            exclude=(),
            max_links=bridge.DEFAULT_MAX_LINKS,
            prune_unmanaged=False,
            link_dir=tmp_path / "links",
        )
        return cfg, bridge.plan_skill_links(cfg)

    def test_links_and_a_manifest_are_created(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        result = bridge.materialize_skill_links(cfg.link_dir, plan)
        assert sorted(result.created) == ["one", "two"]
        assert (cfg.link_dir / "one").is_symlink()
        assert (cfg.link_dir / "one" / "SKILL.md").is_file()
        manifest = bridge.read_manifest(cfg.link_dir)
        assert manifest["version"] == bridge.MANIFEST_VERSION
        assert sorted(entry["name"] for entry in manifest["links"]) == ["one", "two"]

    def test_a_second_run_changes_nothing(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        bridge.materialize_skill_links(cfg.link_dir, plan)
        again = bridge.materialize_skill_links(cfg.link_dir, plan)
        assert again.created == []
        assert again.removed == []
        assert sorted(again.unchanged) == ["one", "two"]

    def test_a_managed_link_whose_target_moved_is_repaired(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        bridge.materialize_skill_links(cfg.link_dir, plan)
        (cfg.link_dir / "one").unlink()
        (cfg.link_dir / "one").symlink_to(tmp_path / "somewhere-else")
        result = bridge.materialize_skill_links(cfg.link_dir, plan)
        assert result.updated == ["one"]
        assert (cfg.link_dir / "one").resolve() == (
            tmp_path / "root" / "cat" / "one"
        ).resolve()

    def test_a_broken_managed_link_self_heals(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        bridge.materialize_skill_links(cfg.link_dir, plan)
        # Exactly the reporter's eight stale links: the target directory moved
        # under a reorg and nothing regenerated them.
        (cfg.link_dir / "one").unlink()
        (cfg.link_dir / "one").symlink_to(tmp_path / "root" / "one")
        assert not (cfg.link_dir / "one").exists()
        bridge.materialize_skill_links(cfg.link_dir, plan)
        assert (cfg.link_dir / "one" / "SKILL.md").is_file()

    def test_a_managed_link_whose_skill_vanished_is_pruned(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        bridge.materialize_skill_links(cfg.link_dir, plan)
        import shutil

        shutil.rmtree(tmp_path / "root" / "cat" / "two")
        second = bridge.plan_skill_links(cfg)
        result = bridge.materialize_skill_links(cfg.link_dir, second)
        # Counted as *broken* rather than merely removed: the link was already
        # dangling by the time this connect looked at it, which is the reporter's
        # eight-stale-links class and worth telling apart in a diagnostic.
        assert result.broken_pruned == ["two"]
        assert result.removed == []
        assert not (cfg.link_dir / "two").exists()
        assert not (cfg.link_dir / "two").is_symlink()
        assert [entry["name"] for entry in
                bridge.read_manifest(cfg.link_dir)["links"]] == ["one"]

    def test_an_unmanaged_real_directory_is_never_touched(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        cfg.link_dir.mkdir(parents=True)
        human = cfg.link_dir / "one"
        human.mkdir()
        (human / "SKILL.md").write_text("mine, by hand", encoding="utf-8")
        result = bridge.materialize_skill_links(cfg.link_dir, plan)
        assert result.skipped_unmanaged == ["one"]
        assert (human / "SKILL.md").read_text(encoding="utf-8") == "mine, by hand"
        assert not human.is_symlink()
        assert result.created == ["two"]

    def test_an_unmanaged_symlink_is_never_replaced(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        cfg.link_dir.mkdir(parents=True)
        elsewhere = tmp_path / "hand-rolled"
        elsewhere.mkdir()
        (cfg.link_dir / "one").symlink_to(elsewhere)
        result = bridge.materialize_skill_links(cfg.link_dir, plan)
        assert result.skipped_unmanaged == ["one"]
        assert (cfg.link_dir / "one").resolve() == elsewhere.resolve()

    def test_an_unmanaged_entry_is_not_recorded_as_ours(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        cfg.link_dir.mkdir(parents=True)
        (cfg.link_dir / "one").mkdir()
        bridge.materialize_skill_links(cfg.link_dir, plan)
        assert [entry["name"] for entry in
                bridge.read_manifest(cfg.link_dir)["links"]] == ["two"]

    def test_unmanaged_entries_survive_prune_when_prune_is_off(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        bridge.materialize_skill_links(cfg.link_dir, plan)
        stranger = cfg.link_dir / "stranger"
        stranger.symlink_to(tmp_path / "root" / "cat" / "one")
        bridge.materialize_skill_links(cfg.link_dir, plan)
        assert stranger.is_symlink()

    def test_prune_unmanaged_removes_stray_symlinks_only(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        bridge.materialize_skill_links(cfg.link_dir, plan)
        (cfg.link_dir / "stranger").symlink_to(tmp_path / "root" / "cat" / "one")
        real = cfg.link_dir / "handwritten"
        real.mkdir()
        bridge.materialize_skill_links(cfg.link_dir, plan, prune_unmanaged=True)
        assert not (cfg.link_dir / "stranger").is_symlink()
        assert real.is_dir()

    def test_the_manifest_round_trips(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        bridge.materialize_skill_links(cfg.link_dir, plan)
        manifest = bridge.read_manifest(cfg.link_dir)
        entry = next(e for e in manifest["links"] if e["name"] == "one")
        assert entry["skill_rel"] == os.path.join("cat", "one")
        assert entry["source_root"] == str((tmp_path / "root").resolve())
        assert entry["target"] == str((tmp_path / "root" / "cat" / "one").resolve())

    def test_a_corrupt_manifest_is_treated_as_owning_nothing(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        cfg.link_dir.mkdir(parents=True)
        (cfg.link_dir / bridge.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        (cfg.link_dir / "one").mkdir()
        result = bridge.materialize_skill_links(cfg.link_dir, plan)
        assert result.skipped_unmanaged == ["one"]

    def test_a_manifest_from_a_future_version_owns_nothing(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        cfg.link_dir.mkdir(parents=True)
        (cfg.link_dir / bridge.MANIFEST_NAME).write_text(
            json.dumps({"version": 99, "links": [{"name": "one"}]}), encoding="utf-8"
        )
        (cfg.link_dir / "one").symlink_to(tmp_path)
        result = bridge.materialize_skill_links(cfg.link_dir, plan)
        assert result.skipped_unmanaged == ["one"]

    def test_nothing_is_written_into_the_skill_root(self, tmp_path):
        cfg, plan = self._plan_of(tmp_path)
        root = tmp_path / "root"
        before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        bridge.materialize_skill_links(cfg.link_dir, plan)
        after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        assert before == after


# ------------------------------------------------------------------ fail open


class TestFailOpen:
    """A skills bridge failure must never fail a turn."""

    def test_bridge_off_writes_nothing(self, tmp_path, write_config, child_home):
        write_config({})
        result = bridge.bridge_skills()
        assert result is None
        assert not (child_home / ".claude").exists()

    def test_a_read_only_link_dir_logs_once_and_returns_an_error(
        self, tmp_path, write_config, caplog, child_home
    ):
        if os.geteuid() == 0:
            pytest.skip("running as root: a read-only directory is not read-only")
        root = tmp_path / "root"
        _skill(root, "cat", "one")
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        write_config({
            "bridge": "link", "include": ["cat/*"], "roots": [str(root)],
            "link_dir": str(locked / "skills"),
        })
        try:
            with caplog.at_level("WARNING"):
                result = bridge.bridge_skills()
        finally:
            locked.chmod(stat.S_IRWXU)
        assert result is not None
        assert result.error
        assert any("skill" in record.getMessage().lower()
                   for record in caplog.records)

    def test_bridge_skills_never_raises(self, monkeypatch, write_config, child_home):
        write_config({"bridge": "link", "include": ["a"]})
        monkeypatch.setattr(
            bridge, "plan_skill_links",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert bridge.bridge_skills() is not None

    def test_an_enabled_bridge_creates_the_dir_and_links(
        self, tmp_path, write_config, child_home
    ):
        root = tmp_path / "root"
        _skill(root, "cat", "one")
        write_config({"bridge": "link", "include": ["cat/*"],
                      "roots": [str(root)]})
        result = bridge.bridge_skills()
        assert result is not None and not result.error
        assert (child_home / ".claude" / "skills" / "one" / "SKILL.md").is_file()


# ------------------------------------------------------------- tool vocabulary


class TestToolVocabulary:
    """Hermes SKILL.md prose names Hermes' tools; Claude Code has other ones."""

    def test_a_skill_naming_hermes_tools_is_reported(self, tmp_path):
        root = tmp_path / "root"
        _skill(root, "cat", "hermes-flavoured",
               body="Use `read_file` and then `patch` the result.")
        cfg = bridge.BridgeConfig(
            mode=bridge.BRIDGE_LINK, roots=[root],
            include=bridge.parse_selectors(["cat/*"]), exclude=(),
            max_links=bridge.DEFAULT_MAX_LINKS, prune_unmanaged=False,
            link_dir=tmp_path / "links",
        )
        plan = bridge.plan_skill_links(cfg)
        link = _by_name(plan)["hermes-flavoured"]
        assert set(link.hermes_tool_mentions) == {"patch", "read_file"}
        assert plan.tool_vocabulary_notes

    def test_a_tool_agnostic_skill_is_not_reported(self, tmp_path):
        root = tmp_path / "root"
        _skill(root, "cat", "plain", body="Think about it, then answer.")
        cfg = bridge.BridgeConfig(
            mode=bridge.BRIDGE_LINK, roots=[root],
            include=bridge.parse_selectors(["cat/*"]), exclude=(),
            max_links=bridge.DEFAULT_MAX_LINKS, prune_unmanaged=False,
            link_dir=tmp_path / "links",
        )
        plan = bridge.plan_skill_links(cfg)
        assert _by_name(plan)["plain"].hermes_tool_mentions == ()
        assert plan.tool_vocabulary_notes == []

    def test_every_hermes_tool_has_a_documented_claude_counterpart(self):
        # A contract about how two pieces of data relate, not a snapshot: every
        # tool name the detector looks for must be answerable when a user asks
        # "what does the adapter call this instead?".
        assert set(bridge.HERMES_TOOL_VOCABULARY) == set(
            bridge.HERMES_TO_CLAUDE_TOOLS
        )
        for hermes, claude in bridge.HERMES_TO_CLAUDE_TOOLS.items():
            assert claude, hermes

    def test_the_skill_file_is_never_rewritten(self, tmp_path):
        root = tmp_path / "root"
        directory = _skill(root, "cat", "hermes-flavoured",
                           body="Use `search_files` first.")
        before = (directory / "SKILL.md").read_bytes()
        cfg = bridge.BridgeConfig(
            mode=bridge.BRIDGE_LINK, roots=[root],
            include=bridge.parse_selectors(["cat/*"]), exclude=(),
            max_links=bridge.DEFAULT_MAX_LINKS, prune_unmanaged=False,
            link_dir=tmp_path / "links",
        )
        plan = bridge.plan_skill_links(cfg)
        bridge.materialize_skill_links(cfg.link_dir, plan)
        assert (directory / "SKILL.md").read_bytes() == before


# --------------------------------------------------------------- the ordering


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="The fake adapter's process-group reaping is POSIX-only; the bridge "
           "itself is covered by the pure tests above.",
)
class TestSessionOrdering:
    """The adapter must see the links AT ``session/new``, not after it.

    Settled against the pinned adapter rather than assumed: ``createSession`` in
    ``@agentclientprotocol/claude-agent-acp@0.66.0`` builds the SDK ``query()``
    and awaits ``initializationResult()`` inside ``session/new`` /
    ``session/load``, and a live probe confirmed the three facts that matter — a
    skill present before ``session/new`` is discovered, one added afterwards is
    NOT (no ``commands_changed`` arrives), and a second ``session/new`` on the
    same adapter process does pick it up. So the bridge belongs in ``_connect()``
    before the session call, and a link written later is invisible for the life
    of that session.
    """

    def _agent(self, tmp_path, monkeypatch, db, session_id, record):
        from hermes_state import SessionDB  # noqa: F401

        monkeypatch.setenv("FAKE_ACP_MODE", "normal")
        monkeypatch.setenv("FAKE_ACP_RECORD", str(record))
        agent = run_agent.AIAgent(
            api_key="claude-code-acp",
            base_url="acp://claude-code",
            provider="claude-code-acp",
            api_mode="claude_code_acp",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=db,
            session_id=session_id,
        )
        agent.acp_command = sys.executable
        agent.acp_args = [_FAKE_ADAPTER]
        agent.session_cwd = str(tmp_path / "ws")
        agent._skill_nudge_interval = 0
        return agent

    def _run(self, tmp_path, monkeypatch, session_id="acp-skills"):
        from hermes_state import SessionDB

        (tmp_path / "ws").mkdir(exist_ok=True)
        db = SessionDB(db_path=tmp_path / "sessions.db")
        record = tmp_path / f"record-{session_id}.json"
        agent = self._agent(tmp_path, monkeypatch, db, session_id, record)
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()
        return json.loads(record.read_text(encoding="utf-8"))

    def test_configured_skills_are_visible_to_the_adapter_at_session_new(
        self, tmp_path, monkeypatch, write_config, child_home
    ):
        root = tmp_path / "root"
        _skill(root, "software-development", "code-review")
        _skill(root, "productivity", "grilling")
        write_config({
            "bridge": "link",
            "roots": [str(root)],
            "include": ["software-development/code-review",
                        "productivity/grilling"],
        })
        data = self._run(tmp_path, monkeypatch)
        listings = data["skills_dir_listings"]
        assert listings, "the adapter recorded no skills-dir listing"
        assert listings[0]["call"] == "session/new"
        assert listings[0]["entries"] == ["code-review", "grilling"]
        # ...and the child can actually read through the links it saw.
        assert listings[0]["usable"] == ["code-review", "grilling"]

    def test_the_default_leaves_the_child_with_no_skills_dir_at_all(
        self, tmp_path, monkeypatch, write_config, child_home
    ):
        write_config({})
        data = self._run(tmp_path, monkeypatch)
        assert data["skills_dir_listings"][0]["entries"] == []
        assert not (child_home / ".claude" / "skills").exists()

    def test_a_link_deleted_between_connects_is_restored_before_the_next_one(
        self, tmp_path, monkeypatch, write_config, child_home
    ):
        root = tmp_path / "root"
        _skill(root, "cat", "one")
        write_config({"bridge": "link", "roots": [str(root)],
                      "include": ["cat/*"]})
        self._run(tmp_path, monkeypatch, session_id="acp-skills-1")
        link_dir = child_home / ".claude" / "skills"
        (link_dir / "one").unlink()
        data = self._run(tmp_path, monkeypatch, session_id="acp-skills-2")
        assert data["skills_dir_listings"][0]["entries"] == ["one"]

    def test_a_skill_the_config_stopped_naming_is_gone_by_the_next_session_new(
        self, tmp_path, monkeypatch, write_config, child_home
    ):
        root = tmp_path / "root"
        _skill(root, "cat", "one")
        _skill(root, "cat", "two")
        write_config({"bridge": "link", "roots": [str(root)],
                      "include": ["cat/*"]})
        first = self._run(tmp_path, monkeypatch, session_id="acp-skills-1")
        assert first["skills_dir_listings"][0]["entries"] == ["one", "two"]
        write_config({"bridge": "link", "roots": [str(root)],
                      "include": ["cat/one"]})
        second = self._run(tmp_path, monkeypatch, session_id="acp-skills-2")
        assert second["skills_dir_listings"][0]["entries"] == ["one"]

    def test_a_hand_made_entry_in_the_link_dir_survives_a_turn(
        self, tmp_path, monkeypatch, write_config, child_home
    ):
        root = tmp_path / "root"
        _skill(root, "cat", "one")
        link_dir = child_home / ".claude" / "skills"
        link_dir.mkdir(parents=True)
        mine = link_dir / "mine-by-hand"
        mine.mkdir()
        (mine / "SKILL.md").write_text("hand written", encoding="utf-8")
        write_config({"bridge": "link", "roots": [str(root)],
                      "include": ["cat/*"]})
        data = self._run(tmp_path, monkeypatch)
        assert data["skills_dir_listings"][0]["entries"] == ["mine-by-hand", "one"]
        assert (mine / "SKILL.md").read_text(encoding="utf-8") == "hand written"

    def test_the_turn_still_completes_when_the_bridge_cannot_write(
        self, tmp_path, monkeypatch, write_config, child_home
    ):
        if os.geteuid() == 0:
            pytest.skip("running as root: a read-only directory is not read-only")
        root = tmp_path / "root"
        _skill(root, "cat", "one")
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        write_config({
            "bridge": "link", "roots": [str(root)], "include": ["cat/*"],
            "link_dir": str(locked / "skills"),
        })
        try:
            (tmp_path / "ws").mkdir(exist_ok=True)
            from hermes_state import SessionDB

            db = SessionDB(db_path=tmp_path / "sessions.db")
            record = tmp_path / "record.json"
            agent = self._agent(tmp_path, monkeypatch, db, "acp-skills-ro", record)
            try:
                result = agent.run_conversation("hello")
            finally:
                agent.close()
        finally:
            locked.chmod(stat.S_IRWXU)
        assert result["completed"] is True
        assert result["final_response"].strip()

    def test_the_bridge_result_is_kept_on_the_session_for_inspection(
        self, tmp_path, monkeypatch, write_config, child_home
    ):
        from hermes_state import SessionDB

        root = tmp_path / "root"
        _skill(root, "cat", "one")
        write_config({"bridge": "link", "roots": [str(root)],
                      "include": ["cat/*"]})
        (tmp_path / "ws").mkdir(exist_ok=True)
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = self._agent(
            tmp_path, monkeypatch, db, "acp-skills-x", tmp_path / "record.json"
        )
        try:
            agent.run_conversation("hello")
            session = agent._claude_acp_session
            assert session.last_skill_bridge_result is not None
            assert session.last_skill_bridge_result.created == ["one"]
        finally:
            agent.close()
