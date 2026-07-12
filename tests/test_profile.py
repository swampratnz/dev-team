"""Tests for workspace project detection."""

from __future__ import annotations

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
