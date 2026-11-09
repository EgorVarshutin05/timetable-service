#!/usr/bin/env python3
"""Проверка истории коммитов по соглашениям CONTRIBUTING.md.

Режим истории (по умолчанию) проверяет коммиты диапазона ``base..head``:
заголовки по Conventional Commits, отсутствие временных коммитов в ``master``,
merge-коммиты пакетов (footers ``Change`` и ``Refs``, описание, связь с веткой
``change/<id>`` и каталогом OpenSpec change), прямые коммиты в ``master`` только
по ``README.md`` и ``openspec/``. Дополнительно собирает факты для проверяющих:
распределение типов, порядок spec/test/код внутри пакетов, незавершённые ветки.

Режим сообщения (``--message-file``) проверяет одно сообщение коммита; его
использует хук ``commit-msg`` (``--merge`` — для merge-коммита пакета).

Уровни: FAIL — нарушение соглашения; WARN — стиль; INFO — факт без оценки.
Код выхода: 0 — нет FAIL или нет ``--strict``; 1 — есть FAIL при ``--strict``
или в режиме сообщения; 2 — ошибка запуска. Только стандартная библиотека и git.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

TYPES: Final[tuple[str, ...]] = (
    "feat",
    "fix",
    "refactor",
    "test",
    "docs",
    "spec",
    "build",
    "ci",
    "chore",
    "perf",
    "revert",
)
CODE_TYPES: Final[frozenset[str]] = frozenset({"feat", "fix", "refactor", "perf"})
DEFAULT_SCOPES: Final[tuple[str, ...]] = (
    "api",
    "domain",
    "db",
    "broker",
    "client",
    "jobs",
    "infra",
    "e2e",
    "readme",
    "openspec",
)
HEADER_MAX: Final[int] = 72
DIRECT_MASTER_ALLOWED: Final[tuple[str, ...]] = ("README.md", "openspec/")
DIRECT_MASTER_TYPES: Final[frozenset[str]] = frozenset({"docs", "chore"})

HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()\s]+)\))?(?P<bang>!)?: (?P<desc>\S.*)$"
)
FORBIDDEN_RE: Final[re.Pattern[str]] = re.compile(r"^(fixup!|squash!|amend!|wip\b)", re.IGNORECASE)
CHANGE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^wp\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*$")
# Refs принимают и функциональные (FR), и нефункциональные (NFR) требования задания.
FR_RE: Final[re.Pattern[str]] = re.compile(r"\bN?FR-\d{2}\b")
TRAILER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<token>[A-Za-z][A-Za-z0-9-]*|BREAKING CHANGE):[ \t]*(?P<value>.*)$"
)
REVERT_DEFAULT_RE: Final[re.Pattern[str]] = re.compile(r'^Revert "(?P<desc>.+)"$')
PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(r"<[^<>\n]+>")
CYRILLIC_RE: Final[re.Pattern[str]] = re.compile(r"[Ѐ-ӿ]")
AI_MARK_RE: Final[re.Pattern[str]] = re.compile(
    r"^(Co-Authored-By:|Generated with|🤖)", re.IGNORECASE | re.MULTILINE
)
LOG_FORMAT: Final[str] = "%H%x1f%P%x1f%aI%x1f%s%x1f%b%x1e"


class LintError(Exception):
    """Ошибка запуска: репозиторий недоступен или ссылка не разрешается."""


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    date: str
    subject: str
    body: str

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def short(self) -> str:
        return self.sha[:7]


@dataclass(frozen=True)
class Header:
    """Разобранный заголовок Conventional Commits."""

    type: str
    scope: str | None
    bang: bool
    desc: str


def parse_header(header: str) -> Header | None:
    revert = REVERT_DEFAULT_RE.match(header)
    if revert is not None:  # стандартное сообщение git revert приравнивается к type revert
        return Header("revert", None, False, revert["desc"])
    match = HEADER_RE.match(header)
    if match is None:
        return None
    return Header(match["type"], match["scope"], match["bang"] is not None, match["desc"])


@dataclass(frozen=True)
class Finding:
    level: str
    rule: str
    commit: str
    message: str


@dataclass
class PackageStats:
    change: str
    merge: str
    merged_at: str
    branch_found: bool
    commits: int
    types: dict[str, int]
    refs: list[str]
    breaking: bool
    first_code: int | None
    last_code: int | None
    first_spec: int | None
    last_spec: int | None
    first_test: int | None
    last_test: int | None
    spec_order: str
    tests_order: str
    fix_after_feat: int
    ai_marked_commits: int
    started_at: str | None


# --- git -------------------------------------------------------------------


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def git(repo: Path, *args: str) -> str:
    result = _run(repo, *args)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"git {' '.join(args)}: код {result.returncode}"
        raise LintError(detail)
    return result.stdout


def git_ok(repo: Path, *args: str) -> bool:
    return _run(repo, *args).returncode == 0


def git_or_none(repo: Path, *args: str) -> str | None:
    result = _run(repo, *args)
    return result.stdout if result.returncode == 0 else None


def read_commits(repo: Path, *selectors: str) -> list[Commit]:
    out = git(repo, "log", "--reverse", f"--format={LOG_FORMAT}", *selectors)
    commits: list[Commit] = []
    for raw in out.split("\x1e"):
        record = raw.strip("\n")
        if not record:
            continue
        sha, parents, date, subject, body = record.split("\x1f", 4)
        commits.append(Commit(sha, tuple(parents.split()), date, subject, body.strip()))
    return commits


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return git_ok(repo, "merge-base", "--is-ancestor", ancestor, descendant)


def changed_paths(repo: Path, sha: str) -> list[str]:
    return git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split()


def resolve_branch(repo: Path, name: str) -> str | None:
    local = git_or_none(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
    if local and local.strip():
        return local.strip()
    remote = git_or_none(repo, "for-each-ref", "--format=%(objectname)", f"refs/remotes/*/{name}")
    tips = (remote or "").split()
    return tips[0] if tips else None


def change_branches(repo: Path) -> dict[str, str]:
    """Ветки change/* (локальные и удалённые): имя без префикса remote → sha."""
    out = git_or_none(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads/change/",
        "refs/remotes/*/change/",
    )
    branches: dict[str, str] = {}
    for line in (out or "").splitlines():
        ref, _, sha = line.partition(" ")
        if ref.startswith("refs/heads/"):
            name = ref.removeprefix("refs/heads/")
            branches[name] = sha
        else:
            name = "change/" + ref.split("/change/", 1)[1]
            branches.setdefault(name, sha)
    return branches


def change_exists_in_rev(repo: Path, rev: str, change_id: str) -> bool:
    if git_ok(repo, "cat-file", "-e", f"{rev}:openspec/changes/{change_id}"):
        return True
    listing = git_or_none(repo, "ls-tree", "--name-only", rev, "openspec/changes/archive/")
    return any(
        name.rstrip("/").endswith(("-" + change_id, "/" + change_id))
        for name in (listing or "").split()
    )


def change_exists_in_worktree(repo: Path, change_id: str) -> bool:
    if (repo / "openspec" / "changes" / change_id).is_dir():
        return True
    archive = repo / "openspec" / "changes" / "archive"
    return archive.is_dir() and any(p.is_dir() for p in archive.glob(f"*-{change_id}"))


def task_fr_ids(text: str | None) -> set[str] | None:
    return None if text is None else set(FR_RE.findall(text))


# --- разбор сообщений ------------------------------------------------------


def parse_trailers(body: str) -> tuple[str, dict[str, list[str]]]:
    """Отделяет footers (git trailers): последний абзац, целиком из строк Token: value."""
    text = body.strip()
    if not text:
        return "", {}
    paragraphs = re.split(r"\n[ \t]*\n", text)
    trailers: dict[str, list[str]] = {}
    last_key: str | None = None
    for line in paragraphs[-1].splitlines():
        if line[:1] in (" ", "\t") and last_key is not None:
            trailers[last_key][-1] = (trailers[last_key][-1] + " " + line.strip()).strip()
            continue
        match = TRAILER_RE.match(line)
        if match is None:
            return text, {}
        last_key = match["token"].lower()
        trailers.setdefault(last_key, []).append(match["value"].strip())
    return "\n\n".join(paragraphs[:-1]).strip(), trailers


def split_message(text: str) -> tuple[str, str]:
    """Заголовок и тело сообщения без строк-комментариев git (начинаются с #)."""
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return "", ""
    header = lines[0].rstrip()
    body = "\n".join(lines[1:]).strip()
    return header, body


def check_header(
    header: str,
    commit: str,
    scopes: frozenset[str],
    header_lang: str,
    placeholders_fail: bool,
) -> tuple[list[Finding], Header | None]:
    findings: list[Finding] = []
    if FORBIDDEN_RE.match(header):
        findings.append(
            Finding(
                "FAIL",
                "temp-commit",
                commit,
                "временный коммит (fixup!/squash!/wip) в истории — схлопнуть autosquash до слияния",
            )
        )
        return findings, None
    parsed = parse_header(header)
    if parsed is None:
        findings.append(
            Finding(
                "FAIL",
                "header-format",
                commit,
                "заголовок не по Conventional Commits «<type>(<scope>)!: <description>»: "
                f"{header!r}",
            )
        )
        return findings, None
    if parsed.type not in TYPES:
        findings.append(
            Finding(
                "FAIL",
                "header-type",
                commit,
                f"неизвестный type «{parsed.type}»; допустимы: {', '.join(TYPES)}",
            )
        )
    scope = parsed.scope
    if scope is not None and scopes and scope not in scopes:
        findings.append(
            Finding(
                "WARN",
                "header-scope",
                commit,
                f"scope «{scope}» вне рекомендованного списка: {', '.join(sorted(scopes))}",
            )
        )
    if len(header) > HEADER_MAX:
        findings.append(
            Finding(
                "WARN",
                "header-length",
                commit,
                f"заголовок длиннее {HEADER_MAX} символов ({len(header)})",
            )
        )
    if parsed.desc.rstrip().endswith("."):
        findings.append(Finding("WARN", "header-period", commit, "точка в конце заголовка"))
    if header_lang == "en" and CYRILLIC_RE.search(header):
        findings.append(
            Finding(
                "WARN",
                "header-lang",
                commit,
                "заголовок содержит кириллицу; соглашение — английский",
            )
        )
    if PLACEHOLDER_RE.search(header):
        level = "FAIL" if placeholders_fail else "WARN"
        findings.append(
            Finding(level, "header-placeholder", commit, "в заголовке остался плейсхолдер «<…>»")
        )
    return findings, parsed


def check_merge_message(
    body: str,
    commit: str,
    task_frs: set[str] | None,
    change_exists: Callable[[str], bool],
    placeholders_fail: bool,
) -> tuple[list[Finding], str | None, list[str]]:
    """Footers и описание merge-коммита пакета. Возвращает findings, id пакета, FR."""
    findings: list[Finding] = []
    description, trailers = parse_trailers(body)
    change_id: str | None = None

    change_values = trailers.get("change", [])
    if not change_values:
        findings.append(
            Finding(
                "FAIL",
                "merge-change",
                commit,
                "merge-коммит пакета без footer «Change: wpNN-<slug>»",
            )
        )
    else:
        change_id = change_values[0]
        if len(change_values) > 1:
            findings.append(
                Finding(
                    "WARN", "merge-change", commit, "несколько footers Change; учитывается первый"
                )
            )
        if not CHANGE_ID_RE.match(change_id):
            findings.append(
                Finding(
                    "FAIL",
                    "merge-change",
                    commit,
                    f"Change «{change_id}» не вида wp02-detect-lesson-conflicts",
                )
            )
            change_id = None
        elif not change_exists(change_id):
            findings.append(
                Finding(
                    "FAIL",
                    "merge-openspec",
                    commit,
                    f"OpenSpec change openspec/changes/{change_id} не найден "
                    "(ни в changes/, ни в archive/)",
                )
            )

    refs_values = trailers.get("refs", [])
    refs = sorted(set(FR_RE.findall(" ".join(refs_values))))
    if not refs_values:
        findings.append(
            Finding("FAIL", "merge-refs", commit, "merge-коммит пакета без footer «Refs: FR-xx, …»")
        )
    elif not refs:
        findings.append(
            Finding(
                "FAIL", "merge-refs", commit, f"Refs без идентификаторов FR-xx: {refs_values!r}"
            )
        )
    elif task_frs is not None:
        unknown = [ref for ref in refs if ref not in task_frs]
        if unknown:
            findings.append(
                Finding(
                    "FAIL",
                    "merge-refs",
                    commit,
                    f"Refs ссылаются на требования, которых нет в TASK.md: {', '.join(unknown)}",
                )
            )

    if not description:
        findings.append(
            Finding(
                "FAIL", "merge-body", commit, "merge-коммит пакета без описания (тело сообщения)"
            )
        )
    elif PLACEHOLDER_RE.search(description):
        level = "FAIL" if placeholders_fail else "WARN"
        findings.append(
            Finding(level, "merge-placeholder", commit, "в описании остался плейсхолдер «<…>»")
        )
    return findings, change_id, refs


# --- статистика пакета -----------------------------------------------------


def relative_order(
    first: int | None, last: int | None, first_code: int | None, last_code: int | None
) -> str:
    if first is None or last is None:
        return "none"
    if first_code is None or last_code is None:
        return "no-code"
    if last < first_code:
        return "before-code"
    if first > last_code:
        return "after-code"
    return "interleaved"


def package_stats(
    repo: Path, merge: Commit, change_id: str, refs: list[str], branch_found: bool
) -> PackageStats:
    members = [
        c for c in read_commits(repo, f"{merge.parents[0]}..{merge.parents[1]}") if not c.is_merge
    ]
    types: Counter[str] = Counter()
    positions: dict[str, list[int]] = {"code": [], "spec": [], "test": [], "feat": []}
    breaking = False
    fix_after_feat = 0
    ai_marked = 0
    for index, commit in enumerate(members):
        parsed = parse_header(commit.subject)
        if parsed is None:
            types["<invalid>"] += 1
            continue
        ctype = parsed.type
        types[ctype] += 1
        breaking = breaking or parsed.bang
        if ctype in CODE_TYPES:
            positions["code"].append(index)
        if ctype in positions:
            positions[ctype].append(index)
        if ctype == "fix" and positions["feat"] and index > positions["feat"][0]:
            fix_after_feat += 1
        if AI_MARK_RE.search(commit.body):
            ai_marked += 1
    merge_header = parse_header(merge.subject)
    breaking = breaking or (merge_header is not None and merge_header.bang)

    def bounds(key: str) -> tuple[int | None, int | None]:
        values = positions[key]
        return (values[0], values[-1]) if values else (None, None)

    first_code, last_code = bounds("code")
    first_spec, last_spec = bounds("spec")
    first_test, last_test = bounds("test")
    return PackageStats(
        change=change_id,
        merge=merge.short,
        merged_at=merge.date,
        branch_found=branch_found,
        commits=len(members),
        types=dict(sorted(types.items())),
        refs=refs,
        breaking=breaking,
        first_code=first_code,
        last_code=last_code,
        first_spec=first_spec,
        last_spec=last_spec,
        first_test=first_test,
        last_test=last_test,
        spec_order=relative_order(first_spec, last_spec, first_code, last_code),
        tests_order=relative_order(first_test, last_test, first_code, last_code),
        fix_after_feat=fix_after_feat,
        ai_marked_commits=ai_marked,
        started_at=members[0].date if members else None,
    )


# --- режимы ----------------------------------------------------------------


def lint_history(
    repo: Path, base: str, head: str, scopes: frozenset[str], header_lang: str
) -> tuple[list[Finding], dict[str, object]]:
    for rev in (base, head):
        if not git_ok(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"):
            raise LintError(f"ссылка «{rev}» не разрешается в коммит")
    head_sha = git(repo, "rev-parse", f"{head}^{{commit}}").strip()
    findings: list[Finding] = []
    if not is_ancestor(repo, base, head):
        findings.append(
            Finding(
                "FAIL",
                "base-ancestry",
                head_sha[:7],
                f"«{base}» не является предком «{head}»: "
                "история переписана или сдача не от scaffold",
            )
        )
        return findings, {"commits_total": 0}

    commits = read_commits(repo, f"{base}..{head}")
    chain = {c.sha for c in read_commits(repo, "--first-parent", f"{base}..{head}")}
    task_frs = task_fr_ids(git_or_none(repo, "show", f"{head}:TASK.md"))
    if task_frs is None:
        findings.append(
            Finding(
                "INFO",
                "task-missing",
                "",
                "TASK.md не найден в проверяемой ревизии: Refs не сверяются",
            )
        )

    types: Counter[str] = Counter()
    packages: list[PackageStats] = []
    direct_master = 0
    reverse_merges = 0
    breaking_commits = 0
    ai_marked = 0
    for commit in commits:
        if commit.is_merge and commit.sha not in chain:
            reverse_merges += 1
            continue
        header_findings, parsed = check_header(
            commit.subject, commit.short, scopes, header_lang, placeholders_fail=False
        )
        findings.extend(header_findings)
        ctype = parsed.type if parsed is not None else "<invalid>"
        types[ctype] += 1
        if parsed is not None and parsed.bang:
            breaking_commits += 1
        if AI_MARK_RE.search(commit.body):
            ai_marked += 1

        if not commit.is_merge:
            if commit.sha in chain:
                direct_master += 1
                paths = changed_paths(repo, commit.sha)
                outside = [p for p in paths if not p.startswith(DIRECT_MASTER_ALLOWED)]
                if outside:
                    findings.append(
                        Finding(
                            "FAIL",
                            "direct-master",
                            commit.short,
                            "прямой коммит в master с изменениями вне README.md и openspec/: "
                            + ", ".join(outside[:5])
                            + (" …" if len(outside) > 5 else ""),
                        )
                    )
                elif ctype not in DIRECT_MASTER_TYPES and ctype != "<invalid>":
                    findings.append(
                        Finding(
                            "WARN",
                            "direct-master",
                            commit.short,
                            f"прямой коммит в master типа «{ctype}»; допустимы docs и chore",
                        )
                    )
            continue

        merge_findings, change_id, refs = check_merge_message(
            commit.body,
            commit.short,
            task_frs,
            lambda cid: change_exists_in_rev(repo, head, cid),
            placeholders_fail=False,
        )
        findings.extend(merge_findings)
        branch_found = False
        if change_id is not None:
            tip = resolve_branch(repo, f"change/{change_id}")
            if tip is None:
                findings.append(
                    Finding(
                        "WARN",
                        "merge-branch",
                        commit.short,
                        f"ветка change/{change_id} не найдена: удалена или не запушена",
                    )
                )
            else:
                branch_found = True
                if not is_ancestor(repo, commit.parents[1], tip):
                    findings.append(
                        Finding(
                            "FAIL",
                            "merge-branch",
                            commit.short,
                            "второй родитель merge-коммита не принадлежит ветке "
                            f"change/{change_id}",
                        )
                    )
            packages.append(package_stats(repo, commit, change_id, refs, branch_found))

    seen: Counter[str] = Counter(p.change for p in packages)
    for change_id, count in seen.items():
        if count > 1:
            findings.append(
                Finding("INFO", "merge-repeat", "", f"пакет {change_id} слит {count} раза(-а)")
            )

    branches = change_branches(repo)
    unmerged: list[str] = []
    for name, sha in sorted(branches.items()):
        if not CHANGE_ID_RE.match(name.removeprefix("change/")):
            findings.append(
                Finding(
                    "WARN",
                    "branch-name",
                    sha[:7],
                    f"имя ветки «{name}» не по соглашению change/wpNN-<slug>",
                )
            )
        if not is_ancestor(repo, sha, head_sha):
            unmerged.append(name)
    if unmerged:
        findings.append(
            Finding(
                "INFO",
                "branch-unmerged",
                "",
                "ветки change/* не слиты в проверяемую ревизию: " + ", ".join(unmerged),
            )
        )
    if reverse_merges:
        findings.append(
            Finding(
                "INFO", "reverse-merge", "", f"обратных слияний master в ветки: {reverse_merges}"
            )
        )

    stats: dict[str, object] = {
        "commits_total": len(commits),
        "package_merges": len(packages),
        "direct_master_commits": direct_master,
        "reverse_merges": reverse_merges,
        "types": dict(sorted(types.items())),
        "breaking_commits": breaking_commits,
        "ai_marked_commits": ai_marked,
        "first_commit_at": commits[0].date if commits else None,
        "last_commit_at": commits[-1].date if commits else None,
        "packages": [asdict(p) for p in packages],
        "change_branches": sorted(branches),
        "unmerged_change_branches": unmerged,
    }
    return findings, stats


def lint_message(
    repo: Path, message_file: Path, merge: bool, scopes: frozenset[str], header_lang: str
) -> list[Finding]:
    header, body = split_message(message_file.read_text(encoding="utf-8"))
    if not header:
        return [Finding("FAIL", "header-format", "", "пустое сообщение коммита")]
    findings, _ = check_header(header, "", scopes, header_lang, placeholders_fail=True)
    if merge:
        task_md = repo / "TASK.md"
        task_frs = task_fr_ids(task_md.read_text(encoding="utf-8") if task_md.is_file() else None)
        merge_findings, _, _ = check_merge_message(
            body,
            "",
            task_frs,
            lambda cid: change_exists_in_worktree(repo, cid),
            placeholders_fail=True,
        )
        findings.extend(merge_findings)
    return findings


# --- вывод -----------------------------------------------------------------


def render_text(findings: Sequence[Finding], stats: dict[str, object] | None, title: str) -> str:
    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    lines = [title]
    for finding in sorted(findings, key=lambda f: order[f.level]):
        commit = finding.commit or "-------"
        lines.append(f"{finding.level:<4} {commit:<7} {finding.rule}: {finding.message}")
    if stats and stats.get("commits_total"):
        types = stats.get("types")
        if isinstance(types, dict) and types:
            lines.append("Типы: " + ", ".join(f"{k} {v}" for k, v in types.items()))
        packages = stats.get("packages")
        if isinstance(packages, list) and packages:
            lines.append("Пакеты:")
            for raw in packages:
                if not isinstance(raw, dict):
                    continue
                branch = "есть" if raw.get("branch_found") else "нет"
                lines.append(
                    f"  {raw.get('change')} · {raw.get('commits')} коммитов"
                    f" · spec: {raw.get('spec_order')} · tests: {raw.get('tests_order')}"
                    f" · fix после feat: {raw.get('fix_after_feat')} · ветка: {branch}"
                )
    counts = Counter(f.level for f in findings)
    lines.append(f"Итог: {counts['FAIL']} FAIL · {counts['WARN']} WARN · {counts['INFO']} INFO")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Проверка истории коммитов по CONTRIBUTING.md (Conventional Commits, пакеты, footers)."
        )
    )
    parser.add_argument(
        "--repo", default=".", help="путь к репозиторию (по умолчанию текущий каталог)"
    )
    parser.add_argument(
        "--base", default="scaffold-base", help="начало диапазона (по умолчанию scaffold-base)"
    )
    parser.add_argument("--head", default="HEAD", help="конец диапазона (по умолчанию HEAD)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--strict", action="store_true", help="код выхода 1 при наличии FAIL")
    parser.add_argument(
        "--header-lang",
        choices=("en", "any"),
        default="en",
        help="язык заголовка (en: кириллица → WARN)",
    )
    parser.add_argument(
        "--scopes",
        default=",".join(DEFAULT_SCOPES),
        help="рекомендованные scope через запятую; пустая строка отключает проверку",
    )
    parser.add_argument(
        "--message-file", type=Path, help="проверить одно сообщение коммита (режим хука)"
    )
    parser.add_argument(
        "--merge", action="store_true", help="с --message-file: сообщение merge-коммита пакета"
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    scopes = frozenset(s.strip() for s in args.scopes.split(",") if s.strip())
    try:
        if args.message_file is not None:
            findings = lint_message(repo, args.message_file, args.merge, scopes, args.header_lang)
            fails = sum(1 for f in findings if f.level == "FAIL")
            if args.format == "json":
                print(
                    json.dumps(
                        {"findings": [asdict(f) for f in findings]}, ensure_ascii=False, indent=2
                    )
                )
            elif findings:
                print(
                    render_text(findings, None, "lint-commits · сообщение коммита"), file=sys.stderr
                )
            return 1 if fails else 0

        findings, stats = lint_history(repo, args.base, args.head, scopes, args.header_lang)
    except LintError as error:
        print(f"lint-commits: {error}", file=sys.stderr)
        return 2

    fails = sum(1 for f in findings if f.level == "FAIL")
    if args.format == "json":
        payload: dict[str, object] = {
            "base": args.base,
            "head": args.head,
            "findings": [asdict(f) for f in findings],
            "stats": stats,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        title = (
            f"lint-commits · {args.base}..{args.head} · {stats.get('commits_total', 0)} коммитов"
            f" · {stats.get('package_merges', 0)} пакетов"
            f" · {stats.get('direct_master_commits', 0)} прямых коммитов в master"
        )
        print(render_text(findings, stats, title))
    return 1 if (fails and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
