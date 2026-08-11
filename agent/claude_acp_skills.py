"""Hermes skills, made discoverable to a Claude Code ACP session — as links.

Packet 5 slice C. Claude Code discovers *personal* skills as
``<config-dir>/skills/<name>/SKILL.md``, one level deep, and loads only each
skill's ``name`` + ``description`` into context (progressive disclosure). So a
bounded link farm costs roughly N short lines, not N skill bodies, and it is the
right shape for making Hermes' own skills reachable without touching the system
prompt — which on this runtime belongs to the adapter and must stay byte-stable
(AGENTS.md §Prompt Caching Must Not Break).

Three facts settled the design, and each is a measurement rather than a guess:

* **Where.** The adapter child runs with the ``HOME`` Hermes' subprocess
  contract gives it (``hermes_subprocess_env`` → ``apply_subprocess_home_env``),
  which on the reporter's host is the *profile* home. The link dir is therefore
  derived from the child's environment — the same dict ``ClaudeAcpClient`` will
  spawn with — and never from ``Path.home()`` (AGENTS.md §Profiles rule 1). The
  pinned SDK resolves its config dir as
  ``process.env.CLAUDE_CONFIG_DIR ?? homedir()/.claude``, so both are honoured.
  On a non-container host install that home IS the real user home, i.e. the very
  ``~/.claude/skills`` a human edits by hand — which is why every write below is
  manifest-gated and why the default is off.

* **When.** ``createSession`` in ``@agentclientprotocol/claude-agent-acp@0.66.0``
  builds the SDK ``query()`` and awaits ``initializationResult()`` *inside*
  ``session/new`` / ``session/load``. A live probe against that adapter confirmed
  the three consequences: a skill present before ``session/new`` is discovered;
  one added afterwards is **not** (no ``commands_changed`` ever arrives); and a
  second ``session/new`` on the same adapter process does pick it up. Discovery
  is per session creation. So the bridge runs in ``ClaudeAcpSession._connect()``
  before the session call, on every connect — new, rebind, idle respawn — and a
  config change takes effect at the next connect rather than by retiring a
  healthy adapter mid-conversation.

* **What.** Hermes' layout is ``<root>/<category>/<skill>/SKILL.md`` — two
  levels where Claude Code's is one — across three roots holding 256 skills with
  20+ duplicate basenames. A flat farm therefore cannot be collision-free by
  construction, and "link everything" is not an option. ``include`` is an
  explicit allow-list with no default, and names are made unique deterministically.

Two rules shape the rest, and they are the ones ``agent/claude_acp_workspace.py``
and ``agent/claude_acp_permissions.py`` already state:

* **Explicit only.** One config key tree
  (``model.claude_code_acp.skills``), never an env var, never an inference. The
  bridge is off unless a profile owner turned it on, and it links nothing unless
  they named what to link.
* **Every rejection narrows.** A bad selector, a missing root, an unreadable
  ``SKILL.md``, a disabled skill, a target outside the configured roots, an
  overflowing cap, an unreadable config: all drop entries. Nothing here can widen
  what the adapter sees by accident, and nothing here can fail a turn.

**Only names this module's own manifest claims are ever replaced or removed.**
An existing entry the manifest does not claim is left completely alone and
reported as ``skipped_unmanaged``. That is what makes the bridge safe to point
at a directory a human also uses — the reporter's real ``~/.claude/skills``
holds eight hand-made symlinks, stale since a 2026-07-27 reorg, and this module
must be incapable of both breaking them and inheriting their failure mode.
Managed links are regenerated from config on every connect, so the
created-once-and-forgotten class of staleness cannot happen to them.

**Tool vocabulary (read this before adding a skill to ``include``).** AGENTS.md
§Skill authoring standards rule 2 requires Hermes SKILL.md prose to name *Hermes*
tools — ``read_file``, ``terminal``, ``patch``, ``search_files``. Claude Code has
none of those; it has ``Read``, ``Bash``, ``Edit``, ``Grep``. A bridged skill
whose procedure is written in Hermes' vocabulary will instruct the model to call
tools that do not exist in that session. This is a correctness problem, not a
safety one, and it is the reason ``include`` has no default: prefer
tool-agnostic skills. :func:`plan_skill_links` reports which Hermes tools each
bridged skill names (see :data:`HERMES_TO_CLAUDE_TOOLS` for the translation) and
logs them once per connect. It never rewrites a skill — external skill dirs are
externally owned, and a Hermes-side edit of one would be a silent fork.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Where the profile owner configures the bridge. Per-profile by construction —
#: ``config.yaml`` is per-``HERMES_HOME``.
CONFIG_KEYS = ("model", "claude_code_acp", "skills")

#: No filesystem writes, no directory created, no behavioural change. The
#: default, and the answer to every rejection in :func:`resolve_bridge_config`.
BRIDGE_OFF = "off"

#: Materialize symlinks. Deliberately not "copy": a stale copy of a skill is
#: worse than no skill, and copying would write into a directory Hermes does not
#: own on the way back out.
BRIDGE_LINK = "link"

BRIDGE_MODES = (BRIDGE_OFF, BRIDGE_LINK)

#: How many links a bridge may create. A policy bound, not a measurement —
#: Hermes cannot see Claude Code's context, so the honest guarantee is "the plan
#: is no longer than this", which is what the tests assert.
DEFAULT_MAX_LINKS = 40

#: ...and the ceiling a configured value is clamped to, so a typo cannot turn
#: into an unbounded farm.
MAX_LINKS_CEILING = 200

#: The manifest that decides what this module owns. Inside the link dir, so a
#: user looking at the directory can see who made it and what it claims.
MANIFEST_NAME = ".hermes-claude-skills.json"

#: Bumped only when the on-disk shape changes. A manifest this build does not
#: understand owns *nothing*, which is the narrow reading: unknown entries are
#: then treated as a human's and left alone.
MANIFEST_VERSION = 1

#: The deepest ``<root>/…/SKILL.md`` nesting a scan will look at. Hermes uses
#: ``<root>/<category>/<skill>``; a bare ``<root>/<skill>`` is accepted too.
MAX_SCAN_DEPTH = 2

#: Bound on how much of a SKILL.md the vocabulary scan reads. Frontmatter plus a
#: procedure fits comfortably; a runaway file must not become a per-connect cost.
SKILL_MD_SCAN_BYTES = 64 * 1024

#: Hermes' native tool names, as AGENTS.md §Skill authoring standards rule 2
#: requires skill prose to spell them, mapped to what the Claude Code session
#: actually offers. Used to *report*, never to rewrite: a bridged skill that
#: says "use ``patch``" is telling the model to call a tool that does not exist
#: in that session, and the profile owner is the one who gets to decide whether
#: the skill is still worth bridging.
HERMES_TO_CLAUDE_TOOLS: Dict[str, str] = {
    "read_file": "Read",
    "write_file": "Write",
    "patch": "Edit",
    "search_files": "Grep / Glob",
    "terminal": "Bash",
    "execute_code": "Bash",
    "web_search": "WebSearch",
    "web_extract": "WebFetch",
    "delegate_task": "Task",
    "browser_navigate": "(no adapter equivalent)",
    "vision_analyze": "(no adapter equivalent; Read handles images)",
    "remember": "(no adapter equivalent)",
    "recall": "(no adapter equivalent)",
}

#: The names the scanner looks for. Kept in agreement with the translation table
#: by a test — a name we detect but cannot translate would be a report the user
#: cannot act on.
HERMES_TOOL_VOCABULARY = frozenset(HERMES_TO_CLAUDE_TOOLS)

#: Tool names are only recognised inside backticks. AGENTS.md requires skill
#: prose to write them that way, and a bare word like "patch" or "terminal" is
#: ordinary English that would otherwise produce a report on every skill.
_BACKTICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")

#: What a generated link name may contain. Claude Code reads the directory name
#: as the skill's identity, so it has to survive being a path component.
_UNSAFE_NAME_CHARS = re.compile(r"[^a-z0-9._-]+")


# ---------------------------------------------------------------- selectors


@dataclass(frozen=True)
class Selector:
    """One entry of ``include`` / ``exclude``.

    Three forms, and deliberately no more: ``"<category>/<skill>"`` (exact),
    ``"<skill>"`` (bare name, any category), ``"<category>/*"`` (one category).
    A bare ``*``, a ``**``, or a regex would make "what did I actually allow?"
    unanswerable from the config file, which is the property this key exists to
    have.
    """

    raw: str
    category: Optional[str]
    name: Optional[str]

    def matches(self, skill: "DiscoveredSkill") -> bool:
        if self.category is not None and self.category != skill.category:
            return False
        if self.name is not None and self.name != skill.name:
            return False
        return True


def parse_selectors(raw: Iterable[Any]) -> Tuple[Selector, ...]:
    """Parse selector strings in config order, dropping the ones we refuse.

    Never raises. Each rejection logs one WARNING naming the entry — a selector
    silently ignored is a config line the user believes is doing something.
    """
    out: List[Selector] = []
    for entry in raw or ():
        if not isinstance(entry, str):
            logger.warning(
                "Ignoring Claude Code skills selector %r: expected a string", entry
            )
            continue
        text = entry.strip().strip("/")
        if not text:
            continue
        if "**" in text or text == "*":
            logger.warning(
                "Ignoring Claude Code skills selector %r: a recursive or "
                "match-everything selector is not supported — name a skill, a "
                "category/skill, or a category/*", entry,
            )
            continue
        parts = text.split("/")
        if len(parts) == 1:
            if "*" in parts[0]:
                logger.warning(
                    "Ignoring Claude Code skills selector %r: wildcards are only "
                    "allowed as the skill half of category/*", entry,
                )
                continue
            out.append(Selector(raw=entry, category=None, name=parts[0]))
            continue
        if len(parts) != 2:
            logger.warning(
                "Ignoring Claude Code skills selector %r: expected at most one "
                "'/' (category/skill or category/*)", entry,
            )
            continue
        category, name = parts
        if "*" in category or (name != "*" and "*" in name):
            logger.warning(
                "Ignoring Claude Code skills selector %r: the only wildcard form "
                "is category/*", entry,
            )
            continue
        out.append(
            Selector(raw=entry, category=category, name=None if name == "*" else name)
        )
    return tuple(out)


# ------------------------------------------------------------------- config


@dataclass(frozen=True)
class BridgeConfig:
    """The effective bridge settings for this profile, already validated."""

    mode: str
    roots: List[Path]
    include: Tuple[Selector, ...]
    exclude: Tuple[Selector, ...]
    max_links: int
    prune_unmanaged: bool
    #: ``None`` only when the bridge is off. Resolving it costs an environment
    #: build (~2 ms), and the default path must not pay for a feature nobody
    #: turned on; ask :func:`default_link_dir` directly if a diagnostic wants to
    #: report where an off bridge *would* write.
    link_dir: Optional[Path]

    @property
    def enabled(self) -> bool:
        return self.mode == BRIDGE_LINK


def _config_node() -> Any:
    """``model.claude_code_acp.skills`` from config.yaml, or ``None``."""
    try:
        from hermes_cli.config import load_config

        node: Any = load_config()
        for key in CONFIG_KEYS:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node
    except Exception:
        logger.debug("reading the Claude Code skills bridge config failed",
                     exc_info=True)
        return None


def _hermes_skill_roots() -> List[Path]:
    """Hermes' own resolved skill roots — ``get_skills_dir()`` + external dirs."""
    try:
        from agent.skill_utils import get_all_skills_dirs

        return [Path(p) for p in get_all_skills_dirs()]
    except Exception:
        logger.debug("resolving Hermes' skill roots failed", exc_info=True)
        try:
            from hermes_constants import get_skills_dir

            return [get_skills_dir()]
        except Exception:
            return []


def _resolve_roots(configured: Any) -> List[Path]:
    """Validated roots, in config order. ``[]``/absent means Hermes' own roots."""
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, (list, tuple)) or not configured:
        candidates = _hermes_skill_roots()
    else:
        candidates = []
        for entry in configured:
            if not isinstance(entry, str) or not entry.strip():
                logger.warning(
                    "Ignoring Claude Code skills root %r: expected a path", entry
                )
                continue
            candidates.append(Path(os.path.expandvars(entry.strip())).expanduser())

    roots: List[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            if not resolved.is_dir():
                raise NotADirectoryError(str(resolved))
        except Exception:
            logger.warning(
                "Ignoring Claude Code skills root %s: not an existing directory "
                "on this host", candidate,
            )
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _resolve_max_links(configured: Any) -> int:
    if isinstance(configured, bool) or not isinstance(configured, int):
        if configured is not None:
            logger.warning(
                "Ignoring model.claude_code_acp.skills.max_links %r: expected an "
                "integer; using %d", configured, DEFAULT_MAX_LINKS,
            )
        return DEFAULT_MAX_LINKS
    if configured < 0:
        return 0
    if configured > MAX_LINKS_CEILING:
        logger.warning(
            "Clamping model.claude_code_acp.skills.max_links %d to %d",
            configured, MAX_LINKS_CEILING,
        )
        return MAX_LINKS_CEILING
    return configured


def _resolve_link_dir(configured: Any) -> Path:
    if isinstance(configured, str) and configured.strip():
        candidate = Path(os.path.expandvars(configured.strip())).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        logger.warning(
            "Ignoring model.claude_code_acp.skills.link_dir %r: a relative path "
            "resolves against whatever the process cwd happens to be", configured,
        )
    return default_link_dir()


def resolve_bridge_config() -> BridgeConfig:
    """The effective bridge settings, or an off bridge.

    Never raises. An absent key, an unreadable config, a non-mapping value and an
    unknown mode all resolve to :data:`BRIDGE_OFF` — no directory, no manifest,
    no writes.
    """
    node = _config_node()
    if node is None:
        return _disabled_config()
    if not isinstance(node, dict):
        logger.warning(
            "Ignoring model.claude_code_acp.skills %r: expected a mapping", node
        )
        return _disabled_config()

    raw_mode = node.get("bridge", BRIDGE_OFF)
    mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""
    if mode not in BRIDGE_MODES:
        logger.warning(
            "Ignoring model.claude_code_acp.skills.bridge %r: expected one of %s; "
            "the skills bridge stays %s", raw_mode, list(BRIDGE_MODES), BRIDGE_OFF,
        )
        mode = BRIDGE_OFF

    if mode == BRIDGE_OFF:
        # Nothing below is consulted when the bridge is off, and resolving roots
        # would stat directories for a feature that is not running.
        return _disabled_config()

    return BridgeConfig(
        mode=mode,
        roots=_resolve_roots(node.get("roots")),
        include=parse_selectors(node.get("include") or ()),
        exclude=parse_selectors(node.get("exclude") or ()),
        max_links=_resolve_max_links(node.get("max_links")),
        prune_unmanaged=bool(node.get("prune_unmanaged")),
        link_dir=_resolve_link_dir(node.get("link_dir")),
    )


def _disabled_config() -> BridgeConfig:
    return BridgeConfig(
        mode=BRIDGE_OFF,
        roots=[],
        include=(),
        exclude=(),
        max_links=DEFAULT_MAX_LINKS,
        prune_unmanaged=False,
        link_dir=None,
    )


# ------------------------------------------------------------------- where


def child_spawn_env() -> Dict[str, str]:
    """The environment ``ClaudeAcpClient`` will spawn the adapter with.

    One source of truth for "where is the adapter's ``~/.claude``": this is the
    same helper chain the client uses (``hermes_subprocess_env`` →
    ``apply_subprocess_home_env``), so the directory Hermes writes and the
    directory the child reads cannot drift apart.
    """
    from tools.environments.local import hermes_subprocess_env

    return hermes_subprocess_env(inherit_credentials=False)


def default_link_dir(env: Optional[Dict[str, str]] = None) -> Path:
    """``<adapter config dir>/skills`` for the adapter this profile will spawn.

    The pinned SDK resolves its config dir as
    ``process.env.CLAUDE_CONFIG_DIR ?? homedir()/.claude``, and Node's
    ``homedir()`` honours ``HOME`` on POSIX / ``USERPROFILE`` on Windows — so
    both are read from the *child's* env. ``Path.home()`` is the last resort and
    only when the child would inherit no home at all.
    """
    child = dict(env) if env is not None else child_spawn_env()
    config_dir = str(child.get("CLAUDE_CONFIG_DIR") or "").strip()
    if config_dir:
        return Path(config_dir).expanduser() / "skills"
    home = str(child.get("HOME") or child.get("USERPROFILE") or "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return base / ".claude" / "skills"


# --------------------------------------------------------------- discovery


@dataclass(frozen=True)
class DiscoveredSkill:
    """One ``SKILL.md``-bearing directory found under a configured root."""

    root: Path
    category: str
    name: str
    path: Path

    @property
    def rel(self) -> str:
        return os.path.join(self.category, self.name) if self.category else self.name


def _is_skill_dir(path: Path) -> bool:
    """Whether *path* holds a readable ``SKILL.md``.

    Readability is checked by opening one byte, not by ``os.access``: a skill the
    adapter cannot read is a link that would show up in its listing and then fail
    the moment the model tried to load it.
    """
    skill_md = path / "SKILL.md"
    try:
        if not skill_md.is_file():
            return False
        with skill_md.open("rb") as handle:
            handle.read(1)
        return True
    except OSError:
        logger.debug("skipping unreadable skill %s", skill_md, exc_info=True)
        return False


def _iter_dirs(path: Path) -> List[Path]:
    try:
        return sorted((p for p in path.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return []


def discover_skills(
    roots: Sequence[Path], disabled: Optional[Iterable[str]] = None
) -> List[DiscoveredSkill]:
    """Every skill under *roots*, in ``(root order, category, name)`` order.

    Scans ``<root>/<skill>/SKILL.md`` and ``<root>/<category>/<skill>/SKILL.md``
    only — Hermes' layout is the second, and nothing deeper is a skill Claude
    Code could be shown as one name anyway. Skills named in Hermes'
    ``skills.disabled`` are dropped: a skill the profile switched off must not
    come back through another door.
    """
    if disabled is None:
        try:
            from agent.skill_utils import get_disabled_skill_names

            disabled = get_disabled_skill_names()
        except Exception:
            logger.debug("reading skills.disabled failed", exc_info=True)
            disabled = set()
    blocked = {str(name).strip() for name in disabled or () if str(name).strip()}

    found: List[DiscoveredSkill] = []
    for root in roots or ():
        root = Path(root)
        if not root.is_dir():
            continue
        for first in _iter_dirs(root):
            if _is_skill_dir(first):
                if first.name not in blocked:
                    found.append(
                        DiscoveredSkill(root=root, category="", name=first.name,
                                        path=first)
                    )
                continue
            if MAX_SCAN_DEPTH < 2:
                continue
            for second in _iter_dirs(first):
                if not _is_skill_dir(second):
                    continue
                if second.name in blocked:
                    continue
                found.append(
                    DiscoveredSkill(root=root, category=first.name,
                                    name=second.name, path=second)
                )
    return found


# -------------------------------------------------------------------- plan


@dataclass(frozen=True)
class SkillLink:
    """One link the bridge intends to have on disk."""

    name: str
    target: Path
    source_root: Path
    skill_rel: str
    hermes_tool_mentions: Tuple[str, ...] = ()


@dataclass
class SkillLinkPlan:
    """What a connect would link, and everything it decided not to link.

    Every "not linked" bucket is named rather than counted away: AGENTS.md's
    no-silent-truncation rule applies to a cap the user set as much as to one we
    set for them.
    """

    links: List[SkillLink] = field(default_factory=list)
    dropped_over_cap: List[str] = field(default_factory=list)
    unmatched_selectors: List[str] = field(default_factory=list)
    refused_outside_roots: List[str] = field(default_factory=list)
    tool_vocabulary_notes: List[str] = field(default_factory=list)


def _within_roots(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots or ():
        try:
            resolved_root = Path(root).resolve()
        except OSError:
            continue
        if path == resolved_root or resolved_root in path.parents:
            return True
    return False


def _slug(text: str) -> str:
    slug = _UNSAFE_NAME_CHARS.sub("-", str(text).strip().lower()).strip("-.")
    return slug or "skill"


def _root_slugs(roots: Sequence[Path]) -> Dict[Path, str]:
    """A stable, unique short name per root, independent of config order."""
    proposed: Dict[Path, str] = {root: _slug(root.name) for root in roots}
    counts: Dict[str, int] = {}
    for slug in proposed.values():
        counts[slug] = counts.get(slug, 0) + 1
    out: Dict[Path, str] = {}
    for root, slug in proposed.items():
        if counts[slug] > 1:
            digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:6]
            slug = f"{slug}-{digest}"
        out[root] = slug
    return out


def _assign_names(
    skills: Sequence[DiscoveredSkill], roots: Sequence[Path]
) -> Dict[DiscoveredSkill, str]:
    """Deterministic, order-independent, collision-free link names.

    ``<skill>`` when that is unique across the plan; else ``<category>-<skill>``;
    else ``<root-slug>-<category>-<skill>``; and if even that collides (two roots
    whose basenames slugify the same), the root slug already carries a digest of
    the root path. The escalation is recomputed from the whole set each round, so
    reordering the roots cannot change the answer.
    """
    slugs = _root_slugs(roots)
    levels = {skill: 1 for skill in skills}

    def _name_at(skill: DiscoveredSkill, level: int) -> str:
        base = _slug(skill.name)
        if level <= 1:
            return base
        category = _slug(skill.category) if skill.category else ""
        parts = [category, base] if category else [base]
        if level >= 3:
            parts = [slugs.get(skill.root, _slug(skill.root.name))] + parts
        return "-".join(parts)

    for _ in range(3):
        names = {skill: _name_at(skill, levels[skill]) for skill in skills}
        counts: Dict[str, int] = {}
        for name in names.values():
            counts[name] = counts.get(name, 0) + 1
        colliding = [s for s in skills if counts[names[s]] > 1 and levels[s] < 3]
        if not colliding:
            break
        for skill in colliding:
            levels[skill] += 1

    names = {skill: _name_at(skill, levels[skill]) for skill in skills}
    # Last resort: two entries that are still identical after the root slug are
    # two roots that resolve to different paths with the same slug AND the same
    # category/skill. Disambiguate rather than silently drop one.
    seen: Dict[str, int] = {}
    final: Dict[DiscoveredSkill, str] = {}
    for skill in sorted(skills, key=lambda s: (str(s.root), s.category, s.name)):
        name = names[skill]
        if name in seen:
            digest = hashlib.sha256(str(skill.path).encode("utf-8")).hexdigest()[:6]
            name = f"{name}-{digest}"
        seen[name] = 1
        final[skill] = name
    return final


def _hermes_tool_mentions(skill: DiscoveredSkill) -> Tuple[str, ...]:
    """Hermes-native tool names this skill's prose tells the model to call."""
    try:
        text = (skill.path / "SKILL.md").read_text(
            encoding="utf-8", errors="replace"
        )[:SKILL_MD_SCAN_BYTES]
    except OSError:
        return ()
    hits = {
        match.group(1)
        for match in _BACKTICKED.finditer(text)
        if match.group(1) in HERMES_TOOL_VOCABULARY
    }
    return tuple(sorted(hits))


def plan_skill_links(config: BridgeConfig) -> SkillLinkPlan:
    """What this config would link right now. Pure: reads, never writes."""
    plan = SkillLinkPlan()
    if not config.include:
        logger.warning(
            "The Claude Code skills bridge is on but no include selectors are "
            "configured, so nothing is linked. Name skills explicitly in "
            "model.claude_code_acp.skills.include."
        )
        return plan

    found = discover_skills(config.roots)
    matched_selectors: set = set()
    selected: List[DiscoveredSkill] = []
    seen_targets: set = set()
    for skill in found:
        hit = [sel for sel in config.include if sel.matches(skill)]
        if not hit:
            continue
        for sel in hit:
            matched_selectors.add(sel.raw)
        if any(sel.matches(skill) for sel in config.exclude):
            continue
        try:
            target = skill.path.resolve()
        except OSError:
            continue
        if not _within_roots(target, config.roots):
            # A skill dir that resolves outside every configured root would hand
            # the adapter a readable path the profile owner never named. Skills
            # are meant to be readable; *which* tree is readable is still a
            # decision, and this keeps it statable.
            logger.warning(
                "Refusing to bridge skill %r: %s resolves outside every "
                "configured skills root", skill.name, skill.path,
            )
            plan.refused_outside_roots.append(skill.name)
            continue
        if target in seen_targets:
            continue
        seen_targets.add(target)
        selected.append(skill)

    plan.unmatched_selectors = [
        sel.raw for sel in config.include if sel.raw not in matched_selectors
    ]
    if plan.unmatched_selectors:
        # A selector that reaches nothing is a config line the profile owner
        # believes is doing something — a typo'd category, or a skill that moved.
        # Silence here reads as "linked", which is the wrong direction.
        logger.warning(
            "Claude Code skills bridge: %d include selector(s) matched no skill "
            "under %s (%s)",
            len(plan.unmatched_selectors),
            ", ".join(str(root) for root in config.roots) or "no configured root",
            ", ".join(plan.unmatched_selectors),
        )

    if len(selected) > config.max_links:
        dropped = selected[config.max_links:]
        selected = selected[: config.max_links]
        plan.dropped_over_cap = [skill.name for skill in dropped]
        logger.warning(
            "Claude Code skills bridge: keeping %d of %d matched skills "
            "(max_links); dropped %s",
            config.max_links, config.max_links + len(dropped),
            ", ".join(plan.dropped_over_cap),
        )

    names = _assign_names(selected, config.roots)
    for skill in selected:
        mentions = _hermes_tool_mentions(skill)
        plan.links.append(
            SkillLink(
                name=names[skill],
                target=skill.path.resolve(),
                source_root=skill.root,
                skill_rel=skill.rel,
                hermes_tool_mentions=mentions,
            )
        )
        if mentions:
            plan.tool_vocabulary_notes.append(
                "{}: names Hermes tools the Claude Code session does not have "
                "({})".format(
                    names[skill],
                    ", ".join(
                        f"{tool} -> {HERMES_TO_CLAUDE_TOOLS[tool]}"
                        for tool in mentions
                    ),
                )
            )
    for note in plan.tool_vocabulary_notes:
        logger.info("Claude Code skills bridge: %s", note)
    return plan


# ------------------------------------------------------------- materialize


@dataclass
class BridgeResult:
    """What one materialization actually did, name by name."""

    link_dir: Optional[Path] = None
    created: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    broken_pruned: List[str] = field(default_factory=list)
    skipped_unmanaged: List[str] = field(default_factory=list)
    pruned_unmanaged: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    #: Carried through from the plan so one object answers "what did this connect
    #: decide, and what did it decline?" — a diagnostic that has to re-run the
    #: planner to find out would report a different moment than the one that ran.
    dropped_over_cap: List[str] = field(default_factory=list)
    unmatched_selectors: List[str] = field(default_factory=list)
    refused_outside_roots: List[str] = field(default_factory=list)
    tool_vocabulary_notes: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def managed(self) -> List[str]:
        return sorted(self.created + self.updated + self.unchanged)

    def summary(self) -> str:
        return (
            f"{len(self.created)} created, {len(self.updated)} repaired, "
            f"{len(self.unchanged)} unchanged, "
            f"{len(self.removed) + len(self.broken_pruned)} removed, "
            f"{len(self.skipped_unmanaged)} left alone"
        )


def read_manifest(link_dir: Path) -> Dict[str, Any]:
    """The manifest in *link_dir*, or an empty one.

    A missing, unreadable, malformed or future-versioned manifest all read as
    "this module owns nothing here" — the narrow answer, because everything it
    does not own is something it must not touch.
    """
    empty: Dict[str, Any] = {"version": MANIFEST_VERSION, "links": []}
    path = Path(link_dir) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty
    except Exception:
        logger.warning(
            "The Claude Code skills manifest at %s is unreadable; treating every "
            "entry in that directory as not ours", path,
        )
        return empty
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        logger.warning(
            "The Claude Code skills manifest at %s was written by another "
            "version; treating every entry in that directory as not ours", path,
        )
        return empty
    links = data.get("links")
    if not isinstance(links, list):
        return empty
    data["links"] = [entry for entry in links if isinstance(entry, dict)
                     and isinstance(entry.get("name"), str)]
    return data


def _managed_names(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {entry["name"]: entry for entry in manifest.get("links", [])}


def _config_hash(links: Sequence[SkillLink]) -> str:
    payload = json.dumps(
        [[link.name, str(link.target), str(link.source_root), link.skill_rel]
         for link in links],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _write_manifest(link_dir: Path, links: Sequence[SkillLink]) -> None:
    payload = {
        "version": MANIFEST_VERSION,
        "generated_by": "hermes",
        "config_hash": _config_hash(links),
        "link_dir": str(link_dir),
        "links": [
            {
                "name": link.name,
                "target": str(link.target),
                "source_root": str(link.source_root),
                "skill_rel": link.skill_rel,
            }
            for link in sorted(links, key=lambda link: link.name)
        ],
    }
    tmp = Path(link_dir) / f"{MANIFEST_NAME}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n",
                   encoding="utf-8")
    tmp.replace(Path(link_dir) / MANIFEST_NAME)


def _link_target(path: Path) -> Optional[str]:
    try:
        return os.readlink(path)
    except OSError:
        return None


def materialize_skill_links(
    link_dir: Path,
    plan: SkillLinkPlan,
    *,
    prune_unmanaged: bool = False,
) -> BridgeResult:
    """Make *link_dir* hold exactly the plan's links — and nothing else of ours.

    Raises only when the link directory itself cannot be created; that is the one
    failure that means nothing at all can be done here, and the caller
    (:func:`bridge_skills`) turns it into a WARNING and an unbridged session.
    Per-entry failures are collected instead, so one bad link cannot cost the
    other thirty-nine.
    """
    link_dir = Path(link_dir)
    result = BridgeResult(
        link_dir=link_dir,
        dropped_over_cap=list(plan.dropped_over_cap),
        unmatched_selectors=list(plan.unmatched_selectors),
        refused_outside_roots=list(plan.refused_outside_roots),
        tool_vocabulary_notes=list(plan.tool_vocabulary_notes),
    )
    link_dir.mkdir(parents=True, exist_ok=True)

    manifest = read_manifest(link_dir)
    managed = _managed_names(manifest)
    desired = {link.name: link for link in plan.links}
    materialized: List[SkillLink] = []

    # 1. Retire what we own and no longer want. A managed name that is no longer
    #    a symlink is a human's now — leave it and stop claiming it.
    for name in sorted(set(managed) - set(desired)):
        path = link_dir / name
        if path.is_symlink():
            broken = not path.exists()
            try:
                path.unlink()
            except OSError:
                logger.debug("could not remove managed link %s", path, exc_info=True)
                result.failed.append(name)
                continue
            (result.broken_pruned if broken else result.removed).append(name)
        elif path.exists():
            result.skipped_unmanaged.append(name)

    # 2. Make each desired link exist, pointing where the plan says.
    for name in sorted(desired):
        link = desired[name]
        path = link_dir / name
        exists = path.is_symlink() or path.exists()
        if exists and not path.is_symlink():
            # A real directory or file. Never replaced, whatever the manifest
            # says — deleting a human's skill to install ours is the one
            # unrecoverable move available here.
            result.skipped_unmanaged.append(name)
            continue
        if path.is_symlink() and name not in managed:
            result.skipped_unmanaged.append(name)
            continue
        target = str(link.target)
        if path.is_symlink():
            current = _link_target(path)
            if current == target and path.exists():
                result.unchanged.append(name)
                materialized.append(link)
                continue
            try:
                path.unlink()
            except OSError:
                logger.debug("could not replace managed link %s", path, exc_info=True)
                result.failed.append(name)
                continue
            bucket = result.updated
        else:
            bucket = result.created
        try:
            os.symlink(target, path, target_is_directory=True)
        except OSError as exc:
            # Windows without the symlink privilege lands here. Deliberately no
            # copy fallback: a stale copy of a skill is worse than no skill, and
            # it would write into a tree Hermes does not own.
            logger.debug("could not create skill link %s -> %s: %s", path, target, exc)
            result.failed.append(name)
            continue
        bucket.append(name)
        materialized.append(link)

    # 3. Optional, and never the recommendation: sweep symlinks nobody claims.
    #    Real directories and files are out of scope even here.
    if prune_unmanaged:
        for entry in sorted(link_dir.iterdir(), key=lambda p: p.name):
            if entry.name == MANIFEST_NAME or entry.name in desired:
                continue
            if not entry.is_symlink():
                continue
            try:
                entry.unlink()
            except OSError:
                continue
            result.pruned_unmanaged.append(entry.name)

    _write_manifest(link_dir, materialized)

    if result.failed:
        logger.warning(
            "Claude Code skills bridge: could not materialize %d link(s) in %s "
            "(%s); the session runs with the rest",
            len(result.failed), link_dir, ", ".join(result.failed),
        )
    if result.skipped_unmanaged:
        logger.info(
            "Claude Code skills bridge: left %d entry/entries in %s untouched "
            "because they are not ours (%s)",
            len(result.skipped_unmanaged), link_dir,
            ", ".join(result.skipped_unmanaged),
        )
    return result


# --------------------------------------------------------------- the seam


def bridge_skills(config: Optional[BridgeConfig] = None) -> Optional[BridgeResult]:
    """Materialize the configured bridge. ``None`` when the bridge is off.

    The one call ``ClaudeAcpSession._connect()`` makes, and the fail-open
    boundary: a skills bridge failure costs one WARNING and an unbridged session,
    never a turn. Nothing here is allowed to raise.
    """
    try:
        cfg = config if config is not None else resolve_bridge_config()
    except Exception:
        logger.warning(
            "Claude Code skills bridge: the configuration could not be read; "
            "the session runs without a bridge", exc_info=True,
        )
        return BridgeResult(error="config unreadable")
    if not cfg.enabled:
        return None

    try:
        plan = plan_skill_links(cfg)
        result = materialize_skill_links(
            cfg.link_dir, plan, prune_unmanaged=cfg.prune_unmanaged
        )
    except Exception as exc:
        logger.warning(
            "Claude Code skills bridge failed for %s; the session runs without "
            "one: %s", cfg.link_dir, exc,
        )
        return BridgeResult(link_dir=cfg.link_dir, error=str(exc))

    logger.info(
        "Claude Code skills bridge: %s in %s", result.summary(), cfg.link_dir
    )
    return result
