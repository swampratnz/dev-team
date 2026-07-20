"""Tests for workspace project detection."""

from __future__ import annotations

import dev_team.profile as profile_module
from dev_team.execution import InMemoryWorkspace
from dev_team.profile import detect_project


def _ws(*paths):
    return InMemoryWorkspace({p: "x" for p in paths})


def test_detect_node():
    profile = detect_project(_ws("package.json", "index.js"))
    assert profile.kind == "node"
    assert profile.verify_command == ("npm", "test")
    assert profile.setup_command == ("npm", "install")


def test_detect_rust():
    profile = detect_project(_ws("Cargo.toml", "src/main.rs"))
    assert profile.kind == "rust"
    assert profile.verify_command == ("cargo", "test")
    assert profile.setup_command is None


def test_detect_go():
    profile = detect_project(_ws("go.mod", "main.go"))
    assert profile.kind == "go"
    assert profile.verify_command == ("go", "test", "./...")


def test_detect_python_with_requirements():
    profile = detect_project(_ws("pyproject.toml", "requirements.txt"))
    assert profile.kind == "python"
    assert profile.verify_command == ("pytest", "-q")
    assert profile.setup_command == ("pip", "install", "-r", "requirements.txt")


def test_detect_python_without_requirements():
    profile = detect_project(_ws("setup.py"))
    assert profile.kind == "python"
    assert profile.setup_command is None


def test_detect_unknown_falls_back_to_pytest():
    profile = detect_project(_ws("README.md"))
    assert profile.kind == "unknown"
    assert profile.verify_command == ("pytest", "-q")


def test_node_wins_over_python_markers():
    profile = detect_project(_ws("package.json", "pyproject.toml"))
    assert profile.kind == "node"


def test_detect_dotnet_solution():
    profile = detect_project(_ws("MyApp.sln", "src/MyApp/MyApp.csproj"))
    assert profile.kind == "dotnet"
    assert profile.verify_command == ("dotnet", "test")
    assert profile.setup_command == ("dotnet", "restore")
    assert profile.security_scan_command[0:3] == ("dotnet", "list", "package")
    assert "MyApp.sln" in profile.reason


def test_detect_dotnet_root_csproj():
    profile = detect_project(_ws("Tool.csproj"))
    assert profile.kind == "dotnet"


def test_detect_dotnet_global_json():
    profile = detect_project(_ws("global.json"))
    assert profile.kind == "dotnet"


def test_dotnet_wins_over_node_for_fullstack_monolith():
    profile = detect_project(_ws("MyApp.sln", "package.json"))
    assert profile.kind == "dotnet"


def test_nested_csproj_alone_is_not_dotnet_root():
    profile = detect_project(_ws("src/MyApp/MyApp.csproj", "package.json"))
    assert profile.kind == "node"


_LEGACY_CSPROJ = """\
<?xml version="1.0" encoding="utf-8"?>
<Project ToolsVersion="12.0" DefaultTargets="Build"
         xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup>
    <TargetFrameworkVersion>v4.5.2</TargetFrameworkVersion>
  </PropertyGroup>
</Project>
"""

_SDK_CSPROJ = """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
"""


def test_detect_legacy_dotnet_by_packages_config():
    ws = InMemoryWorkspace(
        {
            "MyApp.sln": "x",
            "src/App/App.csproj": _SDK_CSPROJ,
            "src/App/packages.config": "<packages />",
        }
    )
    profile = detect_project(ws)
    assert profile.kind == "dotnet-framework"
    assert profile.verify_command is None
    assert profile.setup_command is None
    assert profile.locally_runnable is False
    assert "packages.config" in profile.reason


def test_detect_legacy_dotnet_by_root_packages_config():
    ws = InMemoryWorkspace({"MyApp.csproj": _SDK_CSPROJ, "packages.config": "<packages />"})
    profile = detect_project(ws)
    assert profile.kind == "dotnet-framework"
    assert "packages.config (legacy NuGet restore)" in profile.reason


def test_detect_legacy_dotnet_by_project_xml():
    ws = InMemoryWorkspace({"MyApp.sln": "x", "src/App/App.csproj": _LEGACY_CSPROJ})
    profile = detect_project(ws)
    assert profile.kind == "dotnet-framework"
    assert profile.locally_runnable is False
    assert "src/App/App.csproj" in profile.reason


def test_detect_sdk_style_dotnet_stays_runnable():
    ws = InMemoryWorkspace({"MyApp.sln": "x", "src/App/App.csproj": _SDK_CSPROJ})
    profile = detect_project(ws)
    assert profile.kind == "dotnet"
    assert profile.verify_command == ("dotnet", "test")
    assert profile.locally_runnable is True


def test_legacy_probe_tolerates_unreadable_csproj():
    class _Flaky(InMemoryWorkspace):
        def read_text(self, path):
            if path.endswith(".csproj"):
                raise OSError("binary junk")
            return super().read_text(path)

    ws = _Flaky({"MyApp.sln": "x", "src/App/App.csproj": _LEGACY_CSPROJ})
    profile = detect_project(ws)
    assert profile.kind == "dotnet"


# --- toolchain-presence check -------------------------------------------


def test_recognised_stack_degrades_when_toolchain_missing(monkeypatch):
    # A recognised manifest (.csproj) whose runtime isn't on this machine's
    # PATH must not propose a verify command guaranteed to fail every task.
    monkeypatch.setattr(profile_module.shutil, "which", lambda _name: None)
    profile = detect_project(_ws("Tool.csproj"))
    assert profile.kind == "dotnet"
    assert profile.verify_command is None
    assert profile.locally_runnable is False
    assert "'dotnet' not found on PATH" in profile.reason


def test_recognised_stack_unaffected_when_toolchain_present(monkeypatch):
    # Byte-identical to today's behaviour once the binary is actually there.
    monkeypatch.setattr(
        profile_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    profile = detect_project(_ws("Tool.csproj"))
    assert profile.kind == "dotnet"
    assert profile.verify_command == ("dotnet", "test")
    assert profile.locally_runnable is True
    assert "not found on PATH" not in profile.reason


def test_toolchain_check_looks_at_setup_command_too(monkeypatch):
    # Python's setup_command (pip) is checked even when the verify binary
    # (pytest) is present, since either one missing dooms the task.
    monkeypatch.setattr(
        profile_module.shutil,
        "which",
        lambda name: None if name == "pip" else f"/usr/bin/{name}",
    )
    profile = detect_project(_ws("requirements.txt"))
    assert profile.kind == "python"
    assert profile.verify_command is None
    assert profile.locally_runnable is False
    assert "'pip' not found on PATH" in profile.reason


def test_toolchain_check_skips_already_degraded_profiles(monkeypatch):
    # Legacy .NET Framework is already locally_runnable=False for its own
    # reason; the toolchain check must not layer a second reason onto it.
    monkeypatch.setattr(profile_module.shutil, "which", lambda _name: None)
    ws = InMemoryWorkspace({"MyApp.csproj": _SDK_CSPROJ, "packages.config": "<packages />"})
    profile = detect_project(ws)
    assert profile.kind == "dotnet-framework"
    assert profile.locally_runnable is False
    assert "not found on PATH" not in profile.reason
