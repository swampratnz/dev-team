"""Tests for the deterministic dead-code probes."""

from __future__ import annotations

from dev_team.deadcode import (
    DeadCodeReport,
    _csproj_compile_items,
    detect_dead_code,
    probe_dormant_directories,
    probe_orphaned_projects,
    probe_unreferenced_sources,
)
from dev_team.execution import CommandResult, FakeCommandRunner, InMemoryWorkspace

_NS = 'xmlns="http://schemas.microsoft.com/developer/msbuild/2003"'


def _legacy_csproj(*includes: str) -> str:
    items = "\n".join(f'    <Compile Include="{i}" />' for i in includes)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Project ToolsVersion="12.0" {_NS}>
  <ItemGroup>
{items}
  </ItemGroup>
</Project>
"""


def _sln(*project_paths: str) -> str:
    lines = [
        f'Project("{{GUID}}") = "P{i}", "{path}", "{{GUID2}}"\nEndProject'
        for i, path in enumerate(project_paths)
    ]
    return "Microsoft Visual Studio Solution File\n" + "\n".join(lines)


def test_compile_items_parses_namespaced_project():
    items = _csproj_compile_items(_legacy_csproj("Program.cs", r"Sub\Helper.cs"))
    assert items == {"Program.cs", r"Sub\Helper.cs"}


def test_compile_items_rejects_wildcards_and_bad_xml():
    assert _csproj_compile_items(_legacy_csproj("**/*.cs")) is None
    assert _csproj_compile_items("not xml <<<") is None
    assert _csproj_compile_items("<Project />") is None


def test_compile_items_ignores_compile_without_include():
    xml = f'<Project {_NS}><ItemGroup><Compile /><Compile Include="A.cs" /></ItemGroup></Project>'
    assert _csproj_compile_items(xml) == {"A.cs"}


def test_unreferenced_sources_finds_dead_file():
    ws = InMemoryWorkspace(
        {
            "App/App.csproj": _legacy_csproj("Program.cs", r"Sub\Used.cs"),
            "App/Program.cs": "class P {}",
            "App/Sub/Used.cs": "class U {}",
            "App/Sub/Orphan.cs": "class O {}",
            "App/obj/Debug/Generated.cs": "class G {}",
            "Elsewhere/NotOwned.cs": "class N {}",
        }
    )
    findings, skipped = probe_unreferenced_sources(ws, ws.list_files())
    assert skipped is None
    assert [f.path for f in findings] == ["App/Sub/Orphan.cs"]
    assert "not referenced by any project" in findings[0].detail


def test_unreferenced_sources_skips_without_projects():
    ws = InMemoryWorkspace({"src/x.py": "x = 1"})
    findings, skipped = probe_unreferenced_sources(ws, ws.list_files())
    assert findings == []
    assert "no .csproj" in skipped


def test_unreferenced_sources_skips_when_only_wildcard_projects():
    ws = InMemoryWorkspace(
        {"App/App.csproj": _legacy_csproj("**/*.cs"), "App/Loose.cs": "class L {}"}
    )
    findings, skipped = probe_unreferenced_sources(ws, ws.list_files())
    assert findings == []
    assert "no analysable legacy project" in skipped


def test_unreferenced_sources_tolerates_unreadable_project():
    class _Flaky(InMemoryWorkspace):
        def read_text(self, path):
            if path == "Bad/Bad.csproj":
                raise OSError("unreadable")
            return super().read_text(path)

    ws = _Flaky(
        {
            "Bad/Bad.csproj": "ignored",
            "App/App.csproj": _legacy_csproj("Program.cs"),
            "App/Program.cs": "class P {}",
        }
    )
    findings, skipped = probe_unreferenced_sources(ws, ws.list_files())
    assert skipped is None
    assert findings == []


def test_root_level_project_owns_root_sources():
    ws = InMemoryWorkspace(
        {
            "App.csproj": _legacy_csproj("Program.cs"),
            "Program.cs": "class P {}",
            "Dead.cs": "class D {}",
        }
    )
    findings, skipped = probe_unreferenced_sources(ws, ws.list_files())
    assert skipped is None
    assert [f.path for f in findings] == ["Dead.cs"]


def test_root_level_project_does_not_own_other_projects_sources():
    # A root-level analysable project owns only root-level files. It must not
    # claim files under a sibling project's directory — here an unanalysable
    # (wildcard/SDK-style) project whose sources it cannot see. The old ""
    # prefix matched startswith("") for every path and mis-flagged them.
    ws = InMemoryWorkspace(
        {
            "App.csproj": _legacy_csproj("Program.cs"),
            "Program.cs": "class P {}",
            "Dead.cs": "class D {}",
            "Other/Other.csproj": _legacy_csproj("**/*.cs"),
            "Other/Compiled.cs": "class C {}",
        }
    )
    findings, skipped = probe_unreferenced_sources(ws, ws.list_files())
    assert skipped is None
    # Only the truly unreferenced root file; Other/Compiled.cs belongs to a
    # project this probe cannot analyse and is left alone.
    assert [f.path for f in findings] == ["Dead.cs"]


def test_subdir_project_does_not_own_stray_root_source():
    # The mirror case: only a root-level project owns root-level files, so a
    # subdirectory project leaves a stray root source alone (owns_root False).
    ws = InMemoryWorkspace(
        {
            "Lib/Lib.csproj": _legacy_csproj("Used.cs"),
            "Lib/Used.cs": "class U {}",
            "Stray.cs": "class S {}",
        }
    )
    findings, skipped = probe_unreferenced_sources(ws, ws.list_files())
    assert skipped is None
    assert findings == []


def test_orphaned_projects_found():
    ws = InMemoryWorkspace(
        {
            "Black Pearl.sln": _sln(r"App\App.csproj"),
            "App/App.csproj": "<Project />",
            "Forgotten/Forgotten.csproj": "<Project />",
        }
    )
    findings, skipped = probe_orphaned_projects(ws, ws.list_files())
    assert skipped is None
    assert [f.path for f in findings] == ["Forgotten/Forgotten.csproj"]


def test_orphaned_projects_skips_without_solutions():
    ws = InMemoryWorkspace({"App/App.csproj": "<Project />"})
    findings, skipped = probe_orphaned_projects(ws, ws.list_files())
    assert findings == []
    assert "no .sln" in skipped


def test_orphaned_projects_tolerates_unreadable_solution():
    class _Flaky(InMemoryWorkspace):
        def read_text(self, path):
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "boom")

    ws = _Flaky({"App.sln": "ignored", "App/App.csproj": "<Project />"})
    findings, skipped = probe_orphaned_projects(ws, ws.list_files())
    assert skipped is None
    assert [f.path for f in findings] == ["App/App.csproj"]


def test_sln_in_subdirectory_resolves_relative_paths():
    ws = InMemoryWorkspace(
        {
            "build/All.sln": _sln(r"..\App\App.csproj"),
            "App/App.csproj": "<Project />",
        }
    )
    findings, skipped = probe_orphaned_projects(ws, ws.list_files())
    assert skipped is None
    assert findings == []


def _epoch_days_ago(days: int, head: int = 2_000_000_000) -> str:
    return str(head - days * 86_400)


def test_dormant_directories_flags_stale_top_level_dir():
    runner = FakeCommandRunner()
    runner.add_rule(
        "git log -1 --format=%ct -- Active",
        CommandResult(["git"], 0, _epoch_days_ago(3), ""),
    )
    runner.add_rule(
        "git log -1 --format=%ct -- Sleepy",
        CommandResult(["git"], 0, _epoch_days_ago(500), ""),
    )
    runner.add_rule(
        "git log -1 --format=%ct -- Untracked",
        CommandResult(["git"], 0, "", ""),
    )
    runner.add_rule(
        "git log -1 --format=%ct",
        CommandResult(["git"], 0, str(2_000_000_000), ""),
    )
    files = ["Active/a.cs", "Sleepy/b.cs", "Untracked/c.cs", "README.md"]
    findings, skipped = probe_dormant_directories(runner, "/repo", files)
    assert skipped is None
    assert [f.path for f in findings] == ["Sleepy/"]
    assert "500 day(s)" in findings[0].detail


def test_dormant_directories_skips_outside_git():
    runner = FakeCommandRunner().add_rule(
        "git log", CommandResult(["git"], 128, "", "fatal: not a git repository")
    )
    findings, skipped = probe_dormant_directories(runner, "/tmp", ["a/b.cs"])
    assert findings == []
    assert "not a git repository" in skipped


def test_detect_dead_code_aggregates_and_renders():
    ws = InMemoryWorkspace(
        {
            "All.sln": _sln(r"App\App.csproj"),
            "App/App.csproj": _legacy_csproj("Program.cs"),
            "App/Program.cs": "class P {}",
            "App/Dead.cs": "class D {}",
        }
    )
    report = detect_dead_code(ws)
    assert report.probes_run == ["unreferenced-sources", "orphaned-projects"]
    assert [f.path for f in report.findings] == ["App/Dead.cs"]
    assert any("no git working directory" in s for s in report.skipped)
    rendered = report.render()
    assert "App/Dead.cs" in rendered
    assert "probe skipped" in rendered
    as_dict = report.to_dict()
    assert as_dict["findings"][0]["path"] == "App/Dead.cs"


def test_detect_dead_code_with_git_runner():
    ws = InMemoryWorkspace({"App/App.csproj": _legacy_csproj("Program.cs")})
    runner = FakeCommandRunner().add_rule(
        "git log", CommandResult(["git"], 0, str(2_000_000_000), "")
    )
    report = detect_dead_code(ws, runner=runner, workdir="/repo")
    assert "dormant-directories" in report.probes_run


def test_detect_dead_code_records_skipped_git_probe():
    ws = InMemoryWorkspace({"src/x.py": "x = 1"})
    runner = FakeCommandRunner().add_rule(
        "git log", CommandResult(["git"], 128, "", "fatal")
    )
    report = detect_dead_code(ws, runner=runner, workdir="/repo")
    assert report.probes_run == []
    assert len(report.skipped) == 3


def test_empty_report_renders_empty():
    assert DeadCodeReport().render() == ""
