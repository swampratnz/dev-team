"""Tests for the real delivery engine."""

from __future__ import annotations

import asyncio
import os

import pytest
from helpers import GateCycleRunner, engine_responses, qa_suite_dict, run

from dev_team.backlog import BacklogStore, ItemStatus
from dev_team.budget import Budget, BudgetExceededError
from dev_team.engine import (
    DeliveryEngine,
    DeliveryOutcome,
    EngineConfig,
    _dod_to_test_report,
    _prior_context,
    _review_from_dod,
)
from dev_team.execution import (
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    CommandResult,
    DryRunCommandRunner,
    FakeCommandRunner,
    InMemoryWorkspace,
    LocalWorkspace,
    SubprocessCommandRunner,
)
from dev_team.git import GitRepo
from dev_team.interaction import Reply, ScriptedChannel
from dev_team.memory import CheckpointStore, RunCheckpoint
from dev_team.models import (
    Design,
    FeatureRequest,
    Implementation,
    Plan,
    SecurityReport,
    Task,
    TaskResult,
    TaskStatus,
)
from dev_team.replan import Replan, ReplanAction
from dev_team.sdk import AgentResult
from dev_team.testing import ScriptedRunner, json_response
from dev_team.trace import Tracer
from dev_team.verification import DoDReport, GateResult, RemoteCIGate


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


class KeyedQueueRunner:
    """Keyed runner where each key maps to a queue that pops (last repeats)."""

    def __init__(self, mapping):
        self.mapping = {k: list(v) for k, v in mapping.items()}
        self.calls = []

    async def run(
        self, prompt, *, system_prompt=None, allowed_tools=None, model=None, cwd=None
    ):
        self.calls.append({"prompt": prompt, "model": model, "cwd": cwd})
        for key, queue in self.mapping.items():
            if system_prompt and key in system_prompt:
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, AgentResult):
                    return item
                return AgentResult(text=item, num_turns=1)
        raise AssertionError(f"no queued response for {system_prompt!r}")


class SeqCommandRunner:
    """Returns a queued sequence of results for pytest; 0 for everything else."""

    def __init__(self, pytest_results):
        self.pytest = list(pytest_results)
        self.calls = []

    def run(self, command, *, cwd=None, timeout=None):
        args = list(command)
        self.calls.append(args)
        if "pytest" in " ".join(args):
            return self.pytest.pop(0) if len(self.pytest) > 1 else self.pytest[0]
        return CommandResult(args, 0, "", "")


def _engine(runner, **kwargs):
    kwargs.setdefault("workspace", InMemoryWorkspace())
    kwargs.setdefault("command_runner", GateCycleRunner())
    kwargs.setdefault("budget", Budget())
    kwargs.setdefault("tracer", Tracer(clock=_Clock()))
    return DeliveryEngine(runner, **kwargs)


def _request():
    return FeatureRequest(title="Login", description="Add login")


# --- happy path ---------------------------------------------------------


def test_deliver_happy_path():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    ws = InMemoryWorkspace()
    cmd = GateCycleRunner()
    engine = _engine(runner, workspace=ws, command_runner=cmd)
    outcome = run(engine.deliver(_request()))

    assert outcome.success is True
    assert outcome.tasks_complete is True
    assert outcome.task_results[0].task.status is TaskStatus.DONE
    # engineer's file and QA's test file were written for real
    assert "src/x.py" in ws.list_files()
    assert "tests/test_x.py" in ws.list_files()
    # internal bookkeeping is kept out of the reported product files
    assert outcome.workspace_files == ["src/x.py", "tests/test_x.py"]
    assert outcome.security.approved is True
    assert outcome.documentation is not None
    assert outcome.documentation.unverified_claims == []
    assert outcome.reliability.production_ready is True
    assert outcome.deployment is not None
    assert outcome.blackboard.decisions[0].id == "ADR-001"
    assert outcome.budget.meter.call_count > 0
    assert outcome.budget_exhausted is False
    # cross-run memory was persisted
    assert ws.exists(".dev_team/memory.json")


def test_deliver_surfaces_unverified_doc_claims_without_blocking():
    responses = dict(engine_responses())
    responses["technical writer"] = json_response(
        {
            "summary": "d",
            "sections": [],
            "files": [
                {
                    "path": "docs/feature.md",
                    "change_type": "create",
                    "summary": "docs",
                    "content": "See `src/missing.py` for the implementation.",
                }
            ],
        }
    )
    runner = ScriptedRunner(by_system_prompt=responses)
    ws = InMemoryWorkspace()
    cmd = FakeCommandRunner()
    engine = _engine(runner, workspace=ws, git=_git_with_changes(cmd))
    outcome = run(engine.deliver(_request()))

    # advisory only: the run still succeeds, commits, and the doc file lands
    assert outcome.success is True
    assert outcome.committed is True
    assert "docs/feature.md" in ws.list_files()
    assert len(outcome.documentation.unverified_claims) == 1
    assert "src/missing.py" in outcome.documentation.unverified_claims[0]


def test_deliver_records_transcripts_when_recorder_is_set():
    from dev_team.transcripts import TranscriptRecorder, list_transcripts

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    tx = InMemoryWorkspace()
    recorder = TranscriptRecorder(tx, run="deliver-x")
    engine = _engine(runner, transcript_recorder=recorder)
    run(engine.deliver(_request()))
    # each agent that ran left a captured transcript under its role/run
    assert list_transcripts(tx, "deliver-x", "engineer")
    assert list_transcripts(tx, "deliver-x", "product-manager")


def test_deliver_reviewer_sees_actual_file_content():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner)
    run(engine.deliver(_request()))
    review_calls = [
        c for c in runner.calls if "code reviewer" in (c["system_prompt"] or "")
    ]
    # the applied file content (x = 1) is in the review prompt, not just paths
    assert any("x = 1" in c["prompt"] for c in review_calls)


def test_deliver_review_reject_then_approve():
    mapping = dict(
        {
            "product manager": [json_response(__plan())],
            "software architect": [json_response(__design())],
            "senior software engineer": [json_response(__impl())],
            "code reviewer": [json_response(__review(False)), json_response(__review(True))],
            "quality assurance engineer": [json_response(qa_suite_dict())],
            "application security engineer": [json_response(__security())],
            "technical writer": [json_response(__docs())],
            "site reliability engineer": [json_response(__rel())],
            "DevOps engineer": [json_response(__deploy())],
        }
    )
    runner = KeyedQueueRunner(mapping)
    engine = _engine(runner, config=EngineConfig(max_task_attempts=2))
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert outcome.task_results[0].attempts == 2


def test_deliver_rejected_attempt_is_rolled_back():
    responses = engine_responses(review=False)
    runner = ScriptedRunner(by_system_prompt=responses)
    ws = InMemoryWorkspace({"src/x.py": "original"})
    engine = _engine(runner, workspace=ws, config=EngineConfig(max_task_attempts=1))
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False
    # the failed attempt's write was rolled back to the pre-existing content
    assert ws.read_text("src/x.py") == "original"


def test_deliver_gates_fail_then_pass():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    cmd = SeqCommandRunner(
        [
            CommandResult(["pytest"], 1, "", "fail"),
            CommandResult(["pytest"], 0, "ok", ""),
            CommandResult(["pytest"], 1, "FAILED t.py::x - reverted", ""),
        ]
    )
    engine = _engine(runner, command_runner=cmd, config=EngineConfig(max_task_attempts=2))
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert outcome.task_results[0].attempts == 2


def test_deliver_gate_failure_rolls_back_tests_too():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    cmd = SeqCommandRunner([CommandResult(["pytest"], 1, "", "fail")])
    ws = InMemoryWorkspace()
    engine = _engine(
        runner, workspace=ws, command_runner=cmd, config=EngineConfig(max_task_attempts=1)
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False
    # both the implementation and QA's test file were rolled back
    assert "src/x.py" not in ws.list_files()
    assert "tests/test_x.py" not in ws.list_files()


def test_deliver_task_fails_when_review_never_approves():
    responses = engine_responses(review=False)
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner, config=EngineConfig(max_task_attempts=2))
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False
    assert outcome.task_results[0].task.status is TaskStatus.FAILED


def test_deliver_cascade_skip():
    plan = {
        "summary": "s",
        "tasks": [
            {"id": "T1", "title": "first", "description": "", "dependencies": []},
            {"id": "T2", "title": "second", "description": "", "dependencies": ["T1"]},
        ],
    }
    responses = engine_responses(review=False)
    responses["product manager"] = json_response(plan)
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner, config=EngineConfig(max_task_attempts=1))
    outcome = run(engine.deliver(_request()))
    statuses = {tr.task.id: tr.task.status for tr in outcome.task_results}
    assert statuses["T1"] is TaskStatus.FAILED
    assert statuses["T2"] is TaskStatus.FAILED  # skipped -> failed placeholder
    skipped = next(tr for tr in outcome.task_results if tr.task.id == "T2")
    assert skipped.attempts == 0
    assert skipped.implementation is None


def test_deliver_qa_stage_can_be_disabled():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    ws = InMemoryWorkspace()
    engine = _engine(runner, workspace=ws, config=EngineConfig(qa_tests=False))
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert "tests/test_x.py" not in ws.list_files()


# --- commit behaviour ----------------------------------------------------


def _git_with_changes(cmd):
    cmd.add_rule("status --porcelain", CommandResult(["git"], 0, "M  src/x.py", ""))
    return GitRepo(cmd)


def test_deliver_commits_once_after_security_approval():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    cmd = FakeCommandRunner()
    engine = _engine(runner, git=_git_with_changes(cmd))
    outcome = run(engine.deliver(_request()))
    assert outcome.committed is True
    commits = [c for c in cmd.calls if c[:2] == ["git", "commit"]]
    assert len(commits) == 1
    assert "T1" in commits[0][-1]


def test_deliver_security_block_prevents_commit():
    runner = ScriptedRunner(by_system_prompt=engine_responses(security=False))
    cmd = FakeCommandRunner()
    engine = _engine(runner, git=_git_with_changes(cmd))
    outcome = run(engine.deliver(_request()))
    assert outcome.tasks_complete is True
    assert outcome.security.approved is False
    assert outcome.success is False
    assert outcome.committed is False
    assert not any(c[:2] == ["git", "commit"] for c in cmd.calls)


def test_deliver_without_commit_skips_git():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    cmd = FakeCommandRunner()
    engine = _engine(
        runner, git=_git_with_changes(cmd), config=EngineConfig(commit=False)
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.committed is False
    assert not any(c[:2] == ["git", "commit"] for c in cmd.calls)


def test_deliver_no_commit_when_nothing_changed():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    cmd = FakeCommandRunner()  # status --porcelain returns empty -> no changes
    engine = _engine(runner, git=GitRepo(cmd))
    outcome = run(engine.deliver(_request()))
    assert outcome.committed is False


def test_deliver_commit_failure_is_contained():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    cmd = FakeCommandRunner()
    cmd.add_rule("status --porcelain", CommandResult(["git"], 0, "M  src/x.py", ""))
    cmd.add_rule("commit", CommandResult(["git"], 1, "", "boom"))
    engine = _engine(runner, git=GitRepo(cmd))
    outcome = run(engine.deliver(_request()))
    assert outcome.committed is False
    assert outcome.tasks_complete is True  # the run itself is not sunk


def test_commit_failure_restores_banked_wip_commits():
    # In the baseline branch _commit_if_approved soft-resets the tip back to
    # the baseline and then commits; if that commit fails, the WIP commits are
    # left only in the reflog. The fix hard-resets the tip back to the pre-reset
    # HEAD so the banked commits (and a resumable state) return.
    cmd = FakeCommandRunner()
    cmd.add_rule("rev-parse --verify --quiet HEAD", CommandResult(["git"], 0, "TIP", ""))
    cmd.add_rule("commit", CommandResult(["git"], 1, "", "hook rejected the commit"))
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, git=GitRepo(cmd))
    engine._baseline_sha = "BASE"  # a baseline exists, so WIP commits are banked

    task = Task(id="T1", title="t", description="")
    task.status = TaskStatus.DONE
    results = [TaskResult(task=task, attempts=1)]
    committed = engine._commit_if_approved(
        _request(), results, SecurityReport(approved=True, summary="ok")
    )

    assert committed is False
    # the soft reset to BASE was undone by a hard reset back to the captured tip
    assert ["git", "reset", "--soft", "BASE"] in cmd.calls
    assert ["git", "reset", "--hard", "TIP"] in cmd.calls


def test_deliver_no_commit_without_git_or_root():
    # In-memory workspace, no injected GitRepo: there is nowhere to commit.
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    cmd = FakeCommandRunner()
    engine = _engine(runner, command_runner=cmd)
    outcome = run(engine.deliver(_request()))
    assert outcome.committed is False
    assert not any(c and c[0] == "git" for c in cmd.calls)


def test_deliver_reliability_block_fails_success():
    runner = ScriptedRunner(by_system_prompt=engine_responses(reliability=False))
    outcome = run(_engine(runner).deliver(_request()))
    assert outcome.reliability.production_ready is False
    assert outcome.success is False


# --- budget behaviour -----------------------------------------------------


def _costly(payload, cost):
    return AgentResult(text=json_response(payload), cost_usd=cost, num_turns=1)


def test_deliver_budget_exhaustion_stops_gracefully():
    plan = {
        "summary": "s",
        "tasks": [
            {"id": "T1", "title": "a", "description": "", "dependencies": []},
            {"id": "T2", "title": "b", "description": "", "dependencies": []},
        ],
    }
    mapping = {
        "product manager": [json_response(plan)],
        "software architect": [json_response(__design())],
        # the first engineer call blows the budget
        "senior software engineer": [_costly(__impl(), 100.0)],
    }
    runner = KeyedQueueRunner(mapping)
    engine = _engine(
        runner,
        budget=Budget(limit_usd=50.0),
        config=EngineConfig(max_concurrency=1),
    )
    outcome = run(engine.deliver(_request()))

    assert outcome.budget_exhausted is True
    assert outcome.success is False
    # both tasks failed: one mid-flight, one fast-failed before starting
    assert all(tr.task.status is TaskStatus.FAILED for tr in outcome.task_results)
    # specialist stages were skipped gracefully instead of crashing the run
    assert outcome.security is None
    assert outcome.documentation is None
    assert outcome.deployment is None
    assert outcome.committed is False


def test_deliver_budget_death_mid_integration_rolls_back():
    mapping = {
        "product manager": [json_response(__plan())],
        "software architect": [json_response(__design())],
        "senior software engineer": [json_response(__impl())],
        "code reviewer": [json_response(__review(True))],
        # QA's call blows the budget after the implementation was applied
        "quality assurance engineer": [_costly(qa_suite_dict(), 100.0)],
    }
    runner = KeyedQueueRunner(mapping)
    ws = InMemoryWorkspace()
    engine = _engine(runner, workspace=ws, budget=Budget(limit_usd=50.0))
    outcome = run(engine.deliver(_request()))
    assert outcome.budget_exhausted is True
    # the applied-but-unverified implementation was rolled back
    assert "a.py" not in ws.list_files()


def test_deliver_agentic_engineer_raise_discards_dirty_tree(tmp_path, monkeypatch):
    # The agentic mirror of the budget-death rollback above: the engineer
    # edits the shared workdir directly and then the paid call dies. The
    # implement_in_place call sits outside _integrate's rollback scope, so
    # without its own guard the half-written file survives on disk and the
    # next task's _commit_wip would bank it as if it were gated work.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    ws = LocalWorkspace(str(tmp_path))

    class RaisingEngineerRunner(ScriptedRunner):
        async def run(self, prompt, *, system_prompt=None, **kwargs):
            if system_prompt and "senior software engineer" in system_prompt:
                # a real edit to the shared workdir, then the call blows the budget
                (tmp_path / "leaked.py").write_text("leaked = 1\n")
                raise BudgetExceededError(999.0, 1.0)
            return await super().run(prompt, system_prompt=system_prompt, **kwargs)

    runner = RaisingEngineerRunner(by_system_prompt=engine_responses())
    cmd = SubprocessCommandRunner(cwd=str(tmp_path))
    engine = _engine(
        runner,
        workspace=ws,
        command_runner=cmd,
        config=EngineConfig(max_task_attempts=1),
    )
    outcome = run(engine.deliver(_request()))

    assert outcome.budget_exhausted is True
    # the engineer's half-written file was discarded by the rollback (real git
    # reset --hard + clean), so it never survives to pollute a later commit
    assert not (tmp_path / "leaked.py").exists()
    assert "leaked.py" not in outcome.workspace_files
    # the working tree is clean: a subsequent task's _commit_wip has nothing of
    # the failed task's to bank, and it never reached any commit
    assert engine.git.has_changes() is False
    log = cmd.run(
        ["git", "log", "--all", "--pretty=format:%s%n", "--name-only"],
        cwd=str(tmp_path),
    )
    assert "leaked.py" not in log.stdout


# --- checkpoint / resume --------------------------------------------------


def _two_task_plan():
    return {
        "summary": "s",
        "tasks": [
            {"id": "T1", "title": "first", "description": "", "dependencies": []},
            {"id": "T2", "title": "second", "description": "", "dependencies": ["T1"]},
        ],
    }


def test_deliver_resumes_from_checkpoint():
    ws = InMemoryWorkspace()

    # Run 1: T1 passes review, T2 is rejected -> run incomplete, checkpoint kept.
    mapping = {
        "product manager": [json_response(_two_task_plan())],
        "software architect": [json_response(__design())],
        "senior software engineer": [json_response(__impl())],
        "code reviewer": [json_response(__review(True)), json_response(__review(False))],
        "quality assurance engineer": [json_response(qa_suite_dict())],
        "application security engineer": [json_response(__security())],
        "technical writer": [json_response(__docs())],
        "site reliability engineer": [json_response(__rel())],
        "DevOps engineer": [json_response(__deploy())],
    }
    engine1 = _engine(
        KeyedQueueRunner(mapping), workspace=ws, config=EngineConfig(max_task_attempts=1)
    )
    first = run(engine1.deliver(_request()))
    assert first.success is False
    ckpt_path = CheckpointStore(ws)._path_for("Login")
    assert ws.exists(ckpt_path)

    # Run 2: T1 is restored from the checkpoint; only T2 is developed. The
    # plan itself is reused from the checkpoint, so the product manager is
    # never consulted (a missing mapping entry would raise if it were).
    mapping2 = {
        "software architect": [json_response(__design())],
        "senior software engineer": [json_response(__impl())],
        "code reviewer": [json_response(__review(True))],
        "quality assurance engineer": [json_response(qa_suite_dict())],
        "application security engineer": [json_response(__security())],
        "technical writer": [json_response(__docs())],
        "site reliability engineer": [json_response(__rel())],
        "DevOps engineer": [json_response(__deploy())],
    }
    runner2 = KeyedQueueRunner(mapping2)
    engine2 = _engine(runner2, workspace=ws)
    second = run(engine2.deliver(_request()))
    assert second.success is True
    assert second.resumed_task_ids == ["T1"]
    engineer_prompts = [
        c["prompt"] for c in runner2.calls if "Implement the following task" in c["prompt"]
    ]
    # T1 was restored from the checkpoint: every engineer call is for T2 only
    assert engineer_prompts and all("Task T2" in p for p in engineer_prompts)
    # full success clears the checkpoint
    assert not ws.exists(ckpt_path)


def test_deliver_resume_disabled_keeps_no_checkpoint():
    ws = InMemoryWorkspace()
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, workspace=ws, config=EngineConfig(resume=False))
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert not ws.exists(".dev_team/checkpoint.json")


def test_record_progress_guards():
    engine = _engine(ScriptedRunner([]))
    task = next(iter([]), None)
    # no checkpoint loaded -> no-op
    engine._checkpoint = None
    engine._record_progress(task)
    # checkpoint set but store removed -> no-op
    from dev_team.memory import RunCheckpoint

    engine._checkpoint = RunCheckpoint(feature_title="F")
    engine.checkpoints = None
    engine._record_progress(task)
    assert engine._checkpoint.done_task_ids == []


# --- backlog wiring -------------------------------------------------------


def test_deliver_updates_backlog():
    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, workspace=ws, backlog_store=store)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    backlog = store.load()
    assert backlog.epics[0].title == "Login"
    assert backlog.stories[0].status is ItemStatus.DONE


def test_deliver_backlog_marks_failures_blocked():
    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    runner = ScriptedRunner(by_system_prompt=engine_responses(review=False))
    engine = _engine(
        runner, workspace=ws, backlog_store=store, config=EngineConfig(max_task_attempts=1)
    )
    run(engine.deliver(_request()))
    assert store.load().stories[0].status is ItemStatus.BLOCKED


# --- model routing --------------------------------------------------------


def test_role_models_route_per_agent():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner, config=EngineConfig(model="base", role_models={"reviewer": "cheap"})
    )
    run(engine.deliver(_request()))
    by_role = {}
    for call in runner.calls:
        sp = call["system_prompt"] or ""
        if "code reviewer" in sp:
            by_role.setdefault("reviewer", call["model"])
        elif "product manager" in sp:
            by_role.setdefault("manager", call["model"])
    assert by_role == {"reviewer": "cheap", "manager": "base"}


def test_escalation_model_used_on_final_attempt():
    mapping = {
        "product manager": [json_response(__plan())],
        "software architect": [json_response(__design())],
        "senior software engineer": [json_response(__impl())],
        "code reviewer": [json_response(__review(False)), json_response(__review(True))],
        "quality assurance engineer": [json_response(qa_suite_dict())],
        "application security engineer": [json_response(__security())],
        "technical writer": [json_response(__docs())],
        "site reliability engineer": [json_response(__rel())],
        "DevOps engineer": [json_response(__deploy())],
    }
    runner = KeyedQueueRunner(mapping)
    engine = _engine(
        runner,
        config=EngineConfig(max_task_attempts=2, escalation_model="smart"),
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    engineer_models = [
        c["model"] for c in runner.calls if "Implement the following task" in c["prompt"]
    ]
    assert engineer_models == [None, "smart"]


# --- agentic mode ---------------------------------------------------------


def test_agentic_mode_engineer_works_in_workspace_root(tmp_path):
    ws = LocalWorkspace(str(tmp_path))

    class AgenticRunner(KeyedQueueRunner):
        """Engineer 'does the work': writes the file as a tool side effect."""

        async def run(self, prompt, *, system_prompt=None, **kwargs):
            if system_prompt and "senior software engineer" in system_prompt:
                (tmp_path / "src").mkdir(exist_ok=True)
                (tmp_path / "src" / "x.py").write_text("x = 1\n")
            return await super().run(prompt, system_prompt=system_prompt, **kwargs)

    inplace_impl = {
        "summary": "impl",
        "files": [{"path": "src/x.py", "change_type": "create", "summary": "adds x"}],
        "notes": "",
    }
    mapping = {
        "product manager": [json_response(__plan())],
        "software architect": [json_response(__design())],
        "senior software engineer": [json_response(inplace_impl)],
        "code reviewer": [json_response(__review(True))],
        "quality assurance engineer": [json_response(qa_suite_dict())],
        "application security engineer": [json_response(__security())],
        "technical writer": [json_response(__docs())],
        "site reliability engineer": [json_response(__rel())],
        "DevOps engineer": [json_response(__deploy())],
    }
    runner = AgenticRunner(mapping)
    cmd = GateCycleRunner()
    cmd.add_rule("status --porcelain", CommandResult(["git"], 0, "M  src/x.py", ""))
    engine = _engine(
        runner,
        workspace=ws,
        command_runner=cmd,
        config=EngineConfig(allow_dirty_baseline=True),
    )
    assert engine.agentic is True

    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert outcome.committed is True
    # the delivery worked on its own branch, not whatever was checked out
    assert outcome.branch == "dev-team/login"
    assert ["git", "checkout", "-b", "dev-team/login"] in cmd.calls
    # a baseline commit happened before any task ran (dirty pre-existing tree)
    assert any("baseline" in " ".join(c) for c in cmd.calls if c[:2] == ["git", "commit"])
    # a .gitignore was authored for the fresh workspace
    assert ws.exists(".gitignore")
    # the final commit staged a curated path list (add -A is baseline-only)
    assert ["git", "add", "--", "src/x.py"] in cmd.calls
    # the engineer call carried tools and the workspace root as cwd
    eng_call = next(c for c in runner.calls if "in the current working directory" in c["prompt"])
    assert eng_call["cwd"] == engine.workdir
    # reviewer saw the real on-disk content
    review_call = next(c for c in runner.calls if "Review this implementation" in c["prompt"])
    assert "x = 1" in review_call["prompt"]


def test_agentic_rejection_rolls_back_via_git(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    mapping = engine_responses(review=False)
    # in-place implementation summary (content-free)
    mapping["senior software engineer"] = json_response(
        {"summary": "impl", "files": [{"path": "src/x.py", "change_type": "create", "summary": "s"}]}
    )
    runner = ScriptedRunner(by_system_prompt=mapping)
    cmd = FakeCommandRunner()
    engine = _engine(
        runner, workspace=ws, command_runner=cmd, config=EngineConfig(max_task_attempts=1)
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False
    # the failed attempt was rolled back through git
    assert ["git", "reset", "--hard"] in cmd.calls
    assert ["git", "clean", "-fd"] in cmd.calls


def test_agentic_requires_workspace_root():
    with pytest.raises(ValueError):
        DeliveryEngine(
            ScriptedRunner([]),
            workspace=InMemoryWorkspace(),
            command_runner=FakeCommandRunner(),
            config=EngineConfig(agentic=True),
        )


def test_agentic_can_be_disabled_on_local_workspace(tmp_path):
    engine = DeliveryEngine(
        ScriptedRunner([]),
        workspace=LocalWorkspace(str(tmp_path)),
        command_runner=FakeCommandRunner(),
        config=EngineConfig(agentic=False),
    )
    assert engine.agentic is False


# --- defaults & wiring ----------------------------------------------------


def test_default_construction_uses_defaults():
    # In-memory workspace pairs with an honest dry-run command runner.
    engine = DeliveryEngine(ScriptedRunner([]))
    assert engine.workspace is not None
    assert engine.git is not None
    assert engine.budget is not None
    assert isinstance(engine.command_runner.inner, DryRunCommandRunner)
    assert engine.workdir is None
    assert engine.agentic is False


def test_local_workspace_roots_commands_and_git(tmp_path):
    engine = DeliveryEngine(ScriptedRunner([]), workspace=LocalWorkspace(str(tmp_path)))
    inner = engine.command_runner.inner
    assert isinstance(inner, SubprocessCommandRunner)
    assert inner.cwd == str(tmp_path)
    assert engine.git.cwd == str(tmp_path)
    assert engine.workdir == str(tmp_path)


@pytest.mark.parametrize(
    "kwargs",
    [{"max_task_attempts": 0}, {"max_concurrency": 0}, {"json_retries": -1}],
)
def test_engine_config_validation(kwargs):
    with pytest.raises(ValueError):
        EngineConfig(**kwargs)


# --- pure helpers ---------------------------------------------------------


def test_dod_to_test_report():
    passing = DoDReport([GateResult("t", True, "")])
    failing = DoDReport([GateResult("t", False, "")])
    assert _dod_to_test_report(passing).passed is True
    assert _dod_to_test_report(passing).coverage == 100.0
    assert _dod_to_test_report(failing).coverage == 0.0


def test_review_from_dod():
    report = DoDReport([GateResult("tests", False, "boom"), GateResult("lint", True, "")])
    review = _review_from_dod(report)
    assert review.approved is False
    assert "tests: boom" in review.comments[0].message


def test_prior_context_rendering():
    assert _prior_context(None) is None
    assert _prior_context({}) is None
    assert _prior_context({"decisions": [], "artifacts": []}) is None
    snapshot = {
        "decisions": [{"title": "Arch", "decision": "layered"}],
        "artifacts": [{"kind": "plan"}],
    }
    text = _prior_context(snapshot)
    assert "Arch" in text and "1 artifact(s)" in text


def test_delivery_outcome_property_edges():
    # No tasks -> not complete -> not success; budget None -> zero cost.
    outcome = DeliveryOutcome(
        request=_request(),
        plan_summary="p",
        design=Design(overview="o"),
        task_results=[],
    )
    assert outcome.tasks_complete is False
    assert outcome.success is False
    assert outcome.cost_usd == 0.0


# -- tiny JSON payload builders (kept local to avoid helper churn) --------


def __plan():
    return {
        "summary": "s",
        "tasks": [{"id": "T1", "title": "Core", "description": "d", "dependencies": []}],
    }


def __design():
    return {"overview": "o", "components": [], "tech_stack": ["python"], "risks": []}


def __impl():
    return {
        "summary": "impl",
        "files": [{"path": "a.py", "change_type": "create", "summary": "s", "content": "x"}],
        "notes": "",
    }


def __review(ok):
    return {"approved": ok, "summary": "s", "comments": []}


def __security():
    return {"approved": True, "summary": "ok", "findings": []}


def __docs():
    return {"summary": "d", "sections": []}


def __rel():
    return {"production_ready": True, "summary": "r", "slos": [], "risks": [], "runbook": []}


def __deploy():
    return {"environment": "production", "summary": "s", "steps": [], "rollback": []}


# --- residual branch coverage ----------------------------------------------


def test_snapshot_skips_empty_paths_and_overlapping_qa_files():
    # The implementation carries an empty-path change (skipped everywhere) and
    # QA rewrites a path the implementation already touched (already snapshot).
    impl_payload = {
        "summary": "impl",
        "files": [
            {"path": "src/x.py", "change_type": "create", "summary": "s", "content": "x = 1"},
            {"path": "", "change_type": "create", "summary": "bogus", "content": "ignored"},
        ],
        "notes": "",
    }
    qa_payload = {
        "summary": "tests",
        "files": [
            {"path": "src/x.py", "change_type": "modify", "summary": "adds tests inline",
             "content": "x = 1\ndef test_x(): assert x == 1\n"},
            {"path": "", "change_type": "create", "summary": "bogus", "content": ""},
        ],
        "notes": "",
    }
    responses = engine_responses()
    responses["senior software engineer"] = json_response(impl_payload)
    responses["quality assurance engineer"] = json_response(qa_payload)
    ws = InMemoryWorkspace()
    engine = _engine(ScriptedRunner(by_system_prompt=responses), workspace=ws)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert "test_x" in ws.read_text("src/x.py")


def test_finalise_backlog_ignores_unknown_tasks():
    from dev_team.models import Task, TaskResult

    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    engine = _engine(ScriptedRunner([]), workspace=ws, backlog_store=store)
    backlog = store.load()
    orphan = TaskResult(task=next(iter([Task(id="TX", title="t", description="")])), attempts=0)
    engine._finalise_backlog(backlog, {}, [orphan])  # no story registered for TX
    assert store.load().stories == []


# --- v0.4: baseline, branch, setup, and diff behaviour ----------------------


def test_deliver_halts_on_red_baseline():
    ws = InMemoryWorkspace({"src/app.py": "x = 1"})
    cmd = FakeCommandRunner()
    cmd.add_rule("pytest", CommandResult(["pytest"], 1, "", "legacy test broken"))
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, workspace=ws, command_runner=cmd)
    outcome = run(engine.deliver(_request()))
    assert outcome.halted_reason is not None
    assert "baseline quality gates" in outcome.halted_reason
    assert outcome.baseline is not None and outcome.baseline.passed is False
    assert outcome.success is False
    assert outcome.task_results == []
    # no agent was ever paid: the halt happened before planning
    assert runner.calls == []


def test_deliver_proceeds_on_red_baseline_when_allowed():
    ws = InMemoryWorkspace({"src/app.py": "x = 1"})
    cmd = FakeCommandRunner()
    cmd.add_rule("pytest", CommandResult(["pytest"], 1, "", "legacy test broken"))
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        workspace=ws,
        command_runner=cmd,
        config=EngineConfig(require_green_baseline=False, max_task_attempts=1),
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.halted_reason is None
    assert outcome.baseline is not None and outcome.baseline.passed is False
    # gates still fail during integration, so the run completes incomplete
    assert outcome.success is False


def test_deliver_skips_baseline_check_on_empty_workspace():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner)
    outcome = run(engine.deliver(_request()))
    assert outcome.baseline is None
    assert outcome.success is True


def test_baseline_gate_evaluation_runs_off_the_event_loop(monkeypatch):
    # A remote-CI baseline gate polls with blocking time.sleep for up to its
    # whole timeout; the baseline check must dispatch it via asyncio.to_thread
    # so it never starves the event loop. A red baseline halts before any task,
    # isolating the baseline evaluation as the only gate run.
    ws = InMemoryWorkspace({"src/app.py": "x = 1"})
    cmd = FakeCommandRunner()
    cmd.add_rule("pytest", CommandResult(["pytest"], 1, "", "legacy test broken"))
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, workspace=ws, command_runner=cmd)

    dispatched = []
    real_to_thread = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        dispatched.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("dev_team.engine.asyncio.to_thread", spy)
    outcome = run(engine.deliver(_request()))

    assert outcome.halted_reason is not None  # red baseline halts before tasks
    # the baseline evaluation was handed to a worker thread, not run inline
    assert engine.definition_of_done.evaluate in dispatched


def test_deliver_halts_on_dirty_tree(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    cmd = FakeCommandRunner()
    cmd.add_rule("status --porcelain", CommandResult(["git"], 0, "M  src/x.py", ""))
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, workspace=ws, command_runner=cmd)
    outcome = run(engine.deliver(_request()))
    assert outcome.halted_reason is not None
    assert "uncommitted changes" in outcome.halted_reason
    assert runner.calls == []  # halted before any agent spend


def test_deliver_halts_on_setup_failure():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    cmd = FakeCommandRunner()
    cmd.add_rule("npm install", CommandResult(["npm"], 1, "", "registry down"))
    engine = _engine(
        runner,
        command_runner=cmd,
        config=EngineConfig(setup_command=("npm", "install")),
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.halted_reason is not None
    assert "setup command failed" in outcome.halted_reason
    assert "registry down" in outcome.halted_reason


def test_deliver_setup_success_proceeds():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    cmd = GateCycleRunner()
    engine = _engine(
        runner,
        command_runner=cmd,
        config=EngineConfig(setup_command=("pip", "install", "-e", ".")),
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert ["pip", "install", "-e", "."] in cmd.calls


def test_gates_auto_detected_from_workspace():
    ws = InMemoryWorkspace({"package.json": "{}"})
    cmd = GateCycleRunner()
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, workspace=ws, command_runner=cmd)
    assert engine.definition_of_done is None  # deferred until deliver
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    # the auto-detected gate ran npm test, not pytest
    assert any(c[:2] == ["npm", "test"] for c in cmd.calls)
    assert not any(c and c[0] == "pytest" for c in cmd.calls)


def test_injected_dod_wins_over_detection():
    from dev_team.verification import DefinitionOfDone, PredicateGate

    dod = DefinitionOfDone([PredicateGate("always", lambda ctx: True)])
    engine = _engine(ScriptedRunner([]), definition_of_done=dod)
    assert engine.definition_of_done is dod


def test_agentic_no_branch_when_disabled(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    cmd = FakeCommandRunner()
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        workspace=ws,
        command_runner=cmd,
        config=EngineConfig(use_branch=False, write_gitignore=False),
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.branch is None
    assert not ws.exists(".gitignore")
    assert not any(c[:2] == ["git", "checkout"] for c in cmd.calls)


def test_agentic_keeps_existing_gitignore(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    ws.write_text(".gitignore", "custom\n")
    cmd = FakeCommandRunner()
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, workspace=ws, command_runner=cmd)
    run(engine.deliver(_request()))
    content = ws.read_text(".gitignore")
    # the user's entries are kept; the engine appends its bookkeeping ignore
    assert content.startswith("custom\n")
    assert ".dev_team/" in content
    # and ensures local secrets never get swept into the baseline commit
    assert ".env" in content


def test_default_gitignore_covers_bookkeeping_and_secrets():
    ws = InMemoryWorkspace()
    engine = _engine(ScriptedRunner([]), workspace=ws)
    engine._ensure_gitignore()
    content = ws.read_text(".gitignore")
    assert ".dev_team/" in content
    assert ".env" in content
    assert "*.env" in content


def test_ensure_gitignore_uses_line_based_membership():
    ws = InMemoryWorkspace()
    # '.dev_team' appears only in a comment and an unrelated path (a substring
    # scan would wrongly think it is ignored); '*.env' genuinely globs '.env'.
    ws.write_text(
        ".gitignore",
        "# keep .dev_team notes tidy\n\nlogs/app.dev_team.log\n*.env\n",
    )
    engine = _engine(ScriptedRunner([]), workspace=ws)
    engine._ensure_gitignore()
    content = ws.read_text(".gitignore")
    # a real ignore line is appended for .dev_team/...
    assert "\n.dev_team/\n" in content
    # ...but '.env' is not duplicated, since '*.env' already covers it
    assert [ln.strip() for ln in content.splitlines()].count(".env") == 0


def test_ensure_gitignore_adds_missing_env_only():
    ws = InMemoryWorkspace()
    ws.write_text(".gitignore", ".dev_team/\n")
    engine = _engine(ScriptedRunner([]), workspace=ws)
    engine._ensure_gitignore()
    content = ws.read_text(".gitignore")
    # .dev_team/ already ignored (not re-added), .env was missing and is added
    assert content.count(".dev_team/") == 1
    assert ".env" in content


def test_agentic_unreported_changes_are_reviewed(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    cmd = FakeCommandRunner()
    # git reports an extra file the engineer never mentioned, plus internal noise.
    # status -z is NUL-separated (each record NUL-terminated), so the fake emits
    # that shape rather than newline-delimited lines.
    cmd.add_rule(
        "status --porcelain -uall -z",
        CommandResult(
            ["git"], 0, "M  src/x.py\x00?? src/sneaky.py\x00?? .dev_team/checkpoint.json\x00", ""
        ),
    )
    cmd.add_rule("diff HEAD", CommandResult(["git"], 0, "+++ the-diff-body", ""))
    mapping = engine_responses()
    mapping["senior software engineer"] = json_response(
        {"summary": "impl", "files": [{"path": "src/x.py", "change_type": "modify", "summary": "s"}]}
    )
    runner = ScriptedRunner(by_system_prompt=mapping)
    engine = _engine(
        runner,
        workspace=ws,
        command_runner=cmd,
        config=EngineConfig(allow_dirty_baseline=True),
    )
    outcome = run(engine.deliver(_request()))
    impl = outcome.task_results[0].implementation
    paths = [f.path for f in impl.files]
    assert "src/sneaky.py" in paths  # unreported file was surfaced
    assert ".dev_team/checkpoint.json" not in paths  # internal noise excluded
    review_call = next(c for c in runner.calls if "Review this implementation" in c["prompt"])
    assert "src/sneaky.py" in review_call["prompt"]
    assert "the-diff-body" in review_call["prompt"]  # reviewer got the git diff


def test_gate_timeout_flows_into_gate_context():
    calls = {}

    class RecordingRunner:
        def run(self, command, *, cwd=None, timeout=None):
            calls["timeout"] = timeout
            return CommandResult(list(command), 0, "", "")

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        command_runner=RecordingRunner(),
        config=EngineConfig(gate_timeout_seconds=123.0, fail_to_pass_check=False),
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert calls["timeout"] == 123.0


def test_checkpoint_fingerprint_mismatch_reruns_task():
    from dev_team.memory import RunCheckpoint

    ws = InMemoryWorkspace()
    # a stale checkpoint claims T1 is done, but for different task content
    store = CheckpointStore(ws)
    stale = RunCheckpoint(feature_title="Login")
    stale.mark_done("T1", "0000000000000000")
    store.save(stale)

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, workspace=ws)
    outcome = run(engine.deliver(_request()))
    # fingerprint mismatch -> not resumed, task genuinely developed
    assert outcome.resumed_task_ids == []
    assert outcome.task_results[0].attempts >= 1
    assert outcome.success is True


def test_branch_slug():
    from dev_team.engine import _branch_slug

    assert _branch_slug("Add OAuth 2.0 login!") == "add-oauth-2-0-login"
    assert _branch_slug("???") == "feature"
    assert len(_branch_slug("x" * 100)) <= 40


# --- v0.5: attribution, context, retrospective, worktrees -------------------


class DispatchCommandRunner:
    """Routes pytest results by call order/cwd; records every call with cwd."""

    def __init__(
        self, pytest_results=None, worktree_pytest_ok=True, rev_shas=None, diff_names=None
    ):
        self.pytest = list(pytest_results or [])
        self.worktree_pytest_ok = worktree_pytest_ok
        self.rev = list(rev_shas or [])
        self.diff_names = list(diff_names or [])
        self.calls = []

    def run(self, command, *, cwd=None, timeout=None):
        args = list(command)
        self.calls.append((tuple(args), cwd))
        joined = " ".join(args)
        if "rev-parse --verify --quiet" in joined:
            # GitRepo.rev_parse: dispatch scripted shas; unresolvable without.
            if self.rev:
                sha = self.rev.pop(0) if len(self.rev) > 1 else self.rev[0]
                return CommandResult(args, 0, sha, "")
            return CommandResult(args, 1, "", "")
        if "rev-parse --verify" in joined:
            return CommandResult(args, 0, "ok", "")
        if "diff --name-only" in joined:
            return CommandResult(args, 0, "\n".join(self.diff_names), "")
        # match the verify program itself, not a path that merely contains
        # "pytest" (pytest's own tmp_path does: /tmp/pytest-of-root/...)
        if args and args[0] == "pytest":
            if cwd and "/worktrees/" in str(cwd):
                code = 0 if self.worktree_pytest_ok else 1
                return CommandResult(args, code, "", "")
            if self.pytest:
                return self.pytest.pop(0) if len(self.pytest) > 1 else self.pytest[0]
        return CommandResult(args, 0, "", "")


def _legacy_red(output="FAILED tests/test_legacy.py::test_broken - assert False"):
    return CommandResult(["pytest"], 1, output, "")


def test_tolerated_baseline_accepts_inherited_failures_only():
    ws = InMemoryWorkspace({"src/app.py": "x = 1"})
    cmd = DispatchCommandRunner(pytest_results=[_legacy_red()])
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        workspace=ws,
        command_runner=cmd,
        config=EngineConfig(require_green_baseline=False, fail_to_pass_check=False),
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.halted_reason is None
    assert outcome.success is True  # failing test is inherited, not ours
    report = outcome.task_results[0].test_report
    assert report.passed is True
    assert "pre-existing" in report.summary


def test_tolerated_baseline_still_catches_new_failures():
    ws = InMemoryWorkspace({"src/app.py": "x = 1"})
    baseline = _legacy_red()
    with_new = CommandResult(
        ["pytest"],
        1,
        "FAILED tests/test_legacy.py::test_broken - assert False\n"
        "FAILED tests/test_new.py::test_added - boom",
        "",
    )
    cmd = DispatchCommandRunner(pytest_results=[baseline, with_new])
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        workspace=ws,
        command_runner=cmd,
        config=EngineConfig(require_green_baseline=False, max_task_attempts=1),
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False
    assert outcome.task_results[0].task.status is TaskStatus.FAILED


def test_tolerance_disabled_fails_on_inherited_breakage():
    ws = InMemoryWorkspace({"src/app.py": "x = 1"})
    cmd = DispatchCommandRunner(pytest_results=[_legacy_red()])
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        workspace=ws,
        command_runner=cmd,
        config=EngineConfig(
            require_green_baseline=False,
            tolerate_baseline_failures=False,
            max_task_attempts=1,
        ),
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False


def test_unattributable_baseline_gives_no_tolerance():
    ws = InMemoryWorkspace({"Makefile": "all:"})
    red = CommandResult(["pytest"], 1, "make: *** [all] Error 2", "")
    cmd = DispatchCommandRunner(pytest_results=[red])
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        workspace=ws,
        command_runner=cmd,
        config=EngineConfig(require_green_baseline=False, max_task_attempts=1),
    )
    outcome = run(engine.deliver(_request()))
    # output can't be attributed, so gate failures are never excused
    assert outcome.success is False


def test_brownfield_context_reaches_planner_and_architect():
    ws = InMemoryWorkspace(
        {"README.md": "# Legacy Service", "src/app.py": "x = 1"}
    )
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, workspace=ws)
    run(engine.deliver(_request()))
    pm_prompt = runner.calls[0]["prompt"]
    assert "workspace contains" in pm_prompt
    assert "# Legacy Service" in pm_prompt
    architect_prompt = next(
        c["prompt"] for c in runner.calls if "software architect" in (c["system_prompt"] or "")
    )
    assert "Existing codebase" in architect_prompt
    assert "src/app.py" in architect_prompt


def test_retrospective_persists_and_feeds_next_run():
    ws = InMemoryWorkspace()
    runner = ScriptedRunner(by_system_prompt=engine_responses(review=False))
    engine = _engine(runner, workspace=ws, config=EngineConfig(max_task_attempts=1))
    first = run(engine.deliver(_request()))
    assert first.success is False

    runner2 = ScriptedRunner(by_system_prompt=engine_responses())
    engine2 = _engine(runner2, workspace=ws)
    run(engine2.deliver(_request()))
    pm_prompt = runner2.calls[0]["prompt"]
    assert "last run:" in pm_prompt
    assert "failed after 1 attempt" in pm_prompt


def test_retrospective_notes_hard_won_tasks():
    from dev_team.engine import _retrospective
    from dev_team.models import SecurityReport, Task, TaskResult, TaskStatus

    done = Task(id="T1", title="a", description="", status=TaskStatus.DONE)
    notes = _retrospective(
        [TaskResult(task=done, attempts=3)],
        SecurityReport(approved=False, summary="sqli"),
    )
    assert any("needed 3 attempts" in n for n in notes)
    assert any("security blocked" in n for n in notes)


# --- worktree mode ----------------------------------------------------------


def _worktree_engine(tmp_path, runner, cmd, **config_kwargs):
    return _engine(
        runner,
        workspace=LocalWorkspace(str(tmp_path)),
        command_runner=cmd,
        config=EngineConfig(worktrees=True, **config_kwargs),
    )


def test_worktrees_require_agentic():
    with pytest.raises(ValueError):
        DeliveryEngine(
            ScriptedRunner([]),
            workspace=InMemoryWorkspace(),
            command_runner=FakeCommandRunner(),
            config=EngineConfig(worktrees=True),
        )


def test_worktree_happy_path(tmp_path):
    cmd = DispatchCommandRunner(rev_shas=["BASE", "TIP"])
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _worktree_engine(tmp_path, runner, cmd)
    outcome = run(engine.deliver(_request()))

    assert outcome.success is True
    assert outcome.committed is True
    wt_path = f"{engine.workdir}/.dev_team/worktrees/t1"
    calls = [c for c, _ in cmd.calls]
    assert ("git", "worktree", "add", "-B", "dev-team/login-task-t1", wt_path) in calls
    assert ("git", "merge", "--squash", "dev-team/login-task-t1") in calls
    # WIP commits collapsed into one feature commit at the baseline sha
    assert ("git", "reset", "--soft", "BASE") in calls
    assert any(c[:2] == ("git", "commit") and "Login (T1)" in c[-1] for c in calls)
    # worktree cleaned up afterwards
    assert ("git", "worktree", "remove", "--force", wt_path) in calls
    assert ("git", "branch", "-D", "dev-team/login-task-t1") in calls
    # the engineer worked inside the worktree, not the main checkout
    eng_call = next(
        c for c in runner.calls if "in the current working directory" in c["prompt"]
    )
    assert eng_call["cwd"] == wt_path


def test_worktree_merge_gate_failure_retries(tmp_path):
    # task gates pass in the worktree, but the merged state fails once
    cmd = DispatchCommandRunner(
        pytest_results=[CommandResult(["pytest"], 1, "FAILED t.py::x - boom", ""),
                        CommandResult(["pytest"], 0, "ok", "")],
        rev_shas=["BASE", "TIP"],
    )
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _worktree_engine(tmp_path, runner, cmd, max_task_attempts=2)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert outcome.task_results[0].attempts == 2
    # the failed merge was discarded on the delivery branch
    calls = [c for c, _ in cmd.calls]
    assert ("git", "reset", "--hard") in calls


def test_worktree_no_commit_without_baseline_sha(tmp_path):
    cmd = DispatchCommandRunner()  # rev-parse returns nothing -> no baseline sha
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _worktree_engine(tmp_path, runner, cmd)
    outcome = run(engine.deliver(_request()))
    assert outcome.tasks_complete is True
    assert outcome.committed is False


def test_worktree_review_reject_then_approve(tmp_path):
    mapping = {
        "product manager": [json_response(__plan())],
        "software architect": [json_response(__design())],
        "senior software engineer": [
            json_response({"summary": "impl", "files": [], "notes": ""})
        ],
        "code reviewer": [json_response(__review(False)), json_response(__review(True))],
        "quality assurance engineer": [json_response(qa_suite_dict())],
        "application security engineer": [json_response(__security())],
        "technical writer": [json_response(__docs())],
        "site reliability engineer": [json_response(__rel())],
        "DevOps engineer": [json_response(__deploy())],
    }
    cmd = DispatchCommandRunner(rev_shas=["BASE", "TIP"])
    engine = _worktree_engine(tmp_path, KeyedQueueRunner(mapping), cmd, max_task_attempts=2)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert outcome.task_results[0].attempts == 2


def test_worktree_task_exhausts_attempts(tmp_path):
    cmd = DispatchCommandRunner(rev_shas=["BASE"])
    runner = ScriptedRunner(by_system_prompt=engine_responses(review=False))
    engine = _worktree_engine(tmp_path, runner, cmd, max_task_attempts=1)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False
    assert outcome.task_results[0].task.status is TaskStatus.FAILED
    # the worktree is cleaned up even on failure
    calls = [c for c, _ in cmd.calls]
    assert any(c[:3] == ("git", "worktree", "remove") for c in calls)


def test_worktree_creates_initial_commit_on_empty_repo(tmp_path):
    cmd = FakeCommandRunner()
    cmd.add_rule("rev-parse --verify", CommandResult(["git"], 1, "", "no HEAD"))
    engine = _worktree_engine(tmp_path, ScriptedRunner([]), cmd)
    halted = engine._prepare_git_baseline(_request())
    assert halted is None
    assert ["git", "commit", "--allow-empty", "-m", "chore(dev-team): init"] in cmd.calls


# --- v0.6: research-backed agent upgrades ------------------------------------


def test_plan_lint_triggers_one_revision():
    bad_plan = {
        "summary": "s",
        "tasks": [{"id": "T1", "title": "Core", "description": "d", "dependencies": []}],
    }
    good_plan = {
        "summary": "s",
        "tasks": [
            {
                "id": "T1",
                "title": "Core",
                "description": "d",
                "acceptance_criteria": ["returns 200"],
                "dependencies": [],
            }
        ],
    }
    mapping = {
        "product manager": [json_response(bad_plan), json_response(good_plan)],
        "software architect": [json_response(__design())],
        "senior software engineer": [json_response(__impl())],
        "code reviewer": [json_response(__review(True))],
        "quality assurance engineer": [json_response(qa_suite_dict())],
        "application security engineer": [json_response(__security())],
        "technical writer": [json_response(__docs())],
        "site reliability engineer": [json_response(__rel())],
        "DevOps engineer": [json_response(__deploy())],
    }
    runner = KeyedQueueRunner(mapping)
    engine = _engine(runner)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    pm_prompts = [c["prompt"] for c in runner.calls if "Break the following" in c["prompt"]]
    assert len(pm_prompts) == 2
    assert "previous plan had these problems" in pm_prompts[1]
    assert "no acceptance criteria" in pm_prompts[1]
    assert outcome.blackboard.get("scorecard")["plan_lint_issues"] >= 1


def test_plan_lint_proceeds_after_failed_revision():
    bad_plan = {
        "summary": "s",
        "tasks": [{"id": "T1", "title": "Core", "description": "d", "dependencies": []}],
    }
    responses = engine_responses()
    responses["product manager"] = json_response(bad_plan)  # bad both times
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner)
    outcome = run(engine.deliver(_request()))
    # the run proceeds anyway rather than dying on an imperfect plan
    assert outcome.task_results


def test_architect_receives_prior_decisions():
    ws = InMemoryWorkspace()
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, workspace=ws)
    run(engine.deliver(_request()))
    # second run: the ADR recorded by run 1 must reach the architect
    runner2 = ScriptedRunner(by_system_prompt=engine_responses())
    engine2 = _engine(runner2, workspace=ws)
    run(engine2.deliver(_request()))
    arch_prompt = next(
        c["prompt"] for c in runner2.calls if "software architect" in (c["system_prompt"] or "")
    )
    assert "Prior architecture decisions" in arch_prompt
    assert "Architecture for Login" in arch_prompt


def test_design_rationale_lands_in_adr():
    responses = engine_responses()
    responses["software architect"] = json_response(
        {"overview": "o", "alternatives": ["alt"], "rationale": "THE-RATIONALE"}
    )
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner)
    outcome = run(engine.deliver(_request()))
    assert outcome.design.rationale == "THE-RATIONALE"
    assert outcome.blackboard.decisions[0].consequences == "THE-RATIONALE"


def test_reviewer_receives_lint_findings():
    cmd = GateCycleRunner()
    cmd.add_rule("ruff", CommandResult(["ruff"], 1, "src/x.py:1:1 F401 unused import", ""))
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner, command_runner=cmd, config=EngineConfig(lint_command=("ruff", "check"))
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    review_prompt = next(
        c["prompt"] for c in runner.calls if "code reviewer" in (c["system_prompt"] or "")
    )
    assert "Static analysis output" in review_prompt
    assert "F401" in review_prompt


def test_security_agent_receives_scanner_output():
    cmd = GateCycleRunner()
    cmd.add_rule("bandit", CommandResult(["bandit"], 1, "B602 shell injection risk", ""))
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        command_runner=cmd,
        config=EngineConfig(security_scan_command=("bandit", "-r", ".")),
    )
    run(engine.deliver(_request()))
    sec_prompt = next(
        c["prompt"]
        for c in runner.calls
        if "application security engineer" in (c["system_prompt"] or "")
    )
    assert "Security scanner output" in sec_prompt
    assert "B602" in sec_prompt


def test_security_scan_defaults_from_profile():
    ws = InMemoryWorkspace({"pyproject.toml": "[project]"})
    cmd = GateCycleRunner()
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, workspace=ws, command_runner=cmd)
    run(engine.deliver(_request()))
    # the detected python profile's bandit scan ran through the runner
    assert any(c and c[0] == "bandit" for c in cmd.calls)


def test_security_scanner_not_found_is_not_passed_off_as_findings():
    cmd = GateCycleRunner()
    cmd.add_rule(
        "bandit",
        CommandResult(
            ["bandit"], EXIT_NOT_FOUND, "", "[Errno 2] No such file or directory: 'bandit'"
        ),
    )
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        command_runner=cmd,
        config=EngineConfig(security_scan_command=("bandit", "-r", ".")),
    )
    outcome = run(engine.deliver(_request()))
    sec_prompt = next(
        c["prompt"]
        for c in runner.calls
        if "application security engineer" in (c["system_prompt"] or "")
    )
    assert "No such file or directory" not in sec_prompt
    assert "(no scanner output available" in sec_prompt
    assert outcome.security.scanner_failed is True
    assert "No such file or directory" in outcome.security.scanner_error


def test_security_scanner_timeout_is_not_passed_off_as_findings():
    cmd = GateCycleRunner()
    cmd.add_rule(
        "bandit", CommandResult(["bandit"], EXIT_TIMEOUT, "partial output", "command timed out")
    )
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        command_runner=cmd,
        config=EngineConfig(security_scan_command=("bandit", "-r", ".")),
    )
    outcome = run(engine.deliver(_request()))
    sec_prompt = next(
        c["prompt"]
        for c in runner.calls
        if "application security engineer" in (c["system_prompt"] or "")
    )
    assert "partial output" not in sec_prompt
    assert "(no scanner output available" in sec_prompt
    assert outcome.security.scanner_failed is True
    assert "command timed out" in outcome.security.scanner_error


def test_security_scanner_clean_exit_is_unaffected():
    cmd = GateCycleRunner()
    cmd.add_rule("bandit", CommandResult(["bandit"], 0, "No issues identified.", ""))
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        command_runner=cmd,
        config=EngineConfig(security_scan_command=("bandit", "-r", ".")),
    )
    outcome = run(engine.deliver(_request()))
    sec_prompt = next(
        c["prompt"]
        for c in runner.calls
        if "application security engineer" in (c["system_prompt"] or "")
    )
    assert "No issues identified." in sec_prompt
    assert outcome.security.scanner_failed is False
    assert outcome.security.scanner_error is None


def test_security_scan_command_none_never_invokes_runner():
    cmd = GateCycleRunner()
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        command_runner=cmd,
        config=EngineConfig(security_scan_command=None),
    )
    outcome = run(engine.deliver(_request()))
    assert not any(c and c[0] == "bandit" for c in cmd.calls)
    assert outcome.security.scanner_failed is False
    assert outcome.security.scanner_error is None


def test_security_scanner_failure_text_is_inert_never_reparsed():
    # A stderr payload shaped like a shell-injection/path-traversal attempt
    # must surface only as display text — never re-parsed or used to spawn
    # another command.
    malicious = "[Errno 2] No such file or directory: '; rm -rf / #'"
    cmd = GateCycleRunner()
    cmd.add_rule("bandit", CommandResult(["bandit"], EXIT_NOT_FOUND, "", malicious))
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        command_runner=cmd,
        config=EngineConfig(security_scan_command=("bandit", "-r", ".")),
    )
    outcome = run(engine.deliver(_request()))
    # the scanner command ran exactly once; the malicious-looking text never
    # triggered a second, distinct command invocation
    bandit_calls = [c for c in cmd.calls if c and c[0] == "bandit"]
    assert len(bandit_calls) == 1
    assert outcome.security.scanner_error == malicious
    from dev_team.report import render_delivery_summary

    text = render_delivery_summary(outcome)
    assert "[SCANNER DID NOT RUN]" in text


def test_security_scan_command_invoked_with_unchanged_cwd_timeout():
    class RecordingRunner:
        def __init__(self):
            self.calls = []

        def run(self, command, *, cwd=None, timeout=None, env=None):
            args = list(command)
            self.calls.append({"args": args, "cwd": cwd, "timeout": timeout, "env": env})
            if args and args[0] == "bandit":
                return CommandResult(args, 0, "clean", "")
            if args and args[0] in GateCycleRunner.VERIFY_PROGRAMS:
                return CommandResult(args, 0, "", "")
            return CommandResult(args, 0, "", "")

    cmd = RecordingRunner()
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        command_runner=cmd,
        config=EngineConfig(
            security_scan_command=("bandit", "-r", "."), gate_timeout_seconds=42.0
        ),
    )
    run(engine.deliver(_request()))
    scan_call = next(c for c in cmd.calls if c["args"] and c["args"][0] == "bandit")
    assert scan_call["cwd"] == engine.workdir
    assert scan_call["timeout"] == 42.0


def test_vacuous_tests_are_rejected():
    class AlwaysGreen:
        def __init__(self):
            self.calls = []

        def run(self, command, *, cwd=None, timeout=None):
            self.calls.append(list(command))
            return CommandResult(list(command), 0, "ok", "")

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner, command_runner=AlwaysGreen(), config=EngineConfig(max_task_attempts=1)
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False
    assert outcome.task_results[0].task.status is TaskStatus.FAILED
    report = outcome.task_results[0].test_report
    assert "without the implementation" in report.summary
    assert outcome.blackboard.get("scorecard")["vacuous_test_rejections"] == 1


def test_vacuous_feedback_reaches_engineer():
    class AlwaysGreen:
        def run(self, command, *, cwd=None, timeout=None):
            return CommandResult(list(command), 0, "ok", "")

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner, command_runner=AlwaysGreen(), config=EngineConfig(max_task_attempts=2)
    )
    run(engine.deliver(_request()))
    retry_prompts = [
        c["prompt"] for c in runner.calls if "previous attempt was rejected" in c["prompt"]
    ]
    assert retry_prompts
    assert any("fail on the pre-change code" in p for p in retry_prompts)


def test_fail_to_pass_skipped_for_dry_runs():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = DeliveryEngine(
        runner,
        budget=Budget(),
        tracer=Tracer(clock=_Clock()),
    )  # in-memory + DryRunCommandRunner defaults
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True  # dry runs are not rejected as vacuous


def test_fail_to_pass_agentic_uses_stash(tmp_path):
    ws = LocalWorkspace(str(tmp_path))

    class AgenticRunner(ScriptedRunner):
        async def run(self, prompt, *, system_prompt=None, **kwargs):
            if system_prompt and "senior software engineer" in system_prompt:
                (tmp_path / "src").mkdir(exist_ok=True)
                (tmp_path / "src" / "x.py").write_text("x = 1\n")
            return await super().run(prompt, system_prompt=system_prompt, **kwargs)

    mapping = engine_responses()
    mapping["senior software engineer"] = json_response(
        {"summary": "impl", "files": [{"path": "src/x.py", "change_type": "create", "summary": "s"}]}
    )
    runner = AgenticRunner(by_system_prompt=mapping)
    cmd = GateCycleRunner()
    engine = _engine(runner, workspace=ws, command_runner=cmd)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert ["git", "stash", "push", "-u", "--", "src/x.py"] in cmd.calls
    assert ["git", "stash", "pop"] in cmd.calls


def test_fail_to_pass_skips_when_stash_fails(tmp_path):
    ws = LocalWorkspace(str(tmp_path))

    class AgenticRunner(ScriptedRunner):
        async def run(self, prompt, *, system_prompt=None, **kwargs):
            if system_prompt and "senior software engineer" in system_prompt:
                (tmp_path / "y.py").write_text("y = 1\n")
            return await super().run(prompt, system_prompt=system_prompt, **kwargs)

    mapping = engine_responses()
    mapping["senior software engineer"] = json_response(
        {"summary": "impl", "files": [{"path": "y.py", "change_type": "create", "summary": "s"}]}
    )
    runner = AgenticRunner(by_system_prompt=mapping)
    cmd = GateCycleRunner()
    cmd.add_rule("stash push", CommandResult(["git"], 1, "", "nothing to stash"))
    engine = _engine(runner, workspace=ws, command_runner=cmd)
    outcome = run(engine.deliver(_request()))
    # stash failed -> check skipped rather than falsely rejecting
    assert outcome.success is True
    assert not any(c[:2] == ["git", "stash"] and c[2] == "pop" for c in cmd.calls)


def test_fail_to_pass_conflicting_stash_pop_rejects_as_gate_failure(tmp_path):
    # The implementation is shelved for the fail-to-pass check, but the pop
    # back conflicts. The change is no longer intact on disk, so the attempt
    # must be rejected as a gate failure — never accepted/marked DONE with the
    # implementation silently lost to the stash.
    ws = LocalWorkspace(str(tmp_path))

    class AgenticRunner(ScriptedRunner):
        async def run(self, prompt, *, system_prompt=None, **kwargs):
            if system_prompt and "senior software engineer" in system_prompt:
                (tmp_path / "src").mkdir(exist_ok=True)
                (tmp_path / "src" / "x.py").write_text("x = 1\n")
            return await super().run(prompt, system_prompt=system_prompt, **kwargs)

    mapping = engine_responses()
    mapping["senior software engineer"] = json_response(
        {"summary": "impl", "files": [{"path": "src/x.py", "change_type": "create", "summary": "s"}]}
    )
    runner = AgenticRunner(by_system_prompt=mapping)
    cmd = GateCycleRunner()
    cmd.add_rule(
        "stash pop",
        CommandResult(["git"], 1, "", "CONFLICT (content): merge conflict in src/x.py"),
    )
    engine = _engine(
        runner, workspace=ws, command_runner=cmd, config=EngineConfig(max_task_attempts=1)
    )
    outcome = run(engine.deliver(_request()))

    assert outcome.success is False
    assert outcome.task_results[0].task.status is TaskStatus.FAILED
    # surfaced as a gate failure, not a vacuous-test rejection
    assert outcome.blackboard.get("scorecard")["gate_failures"] >= 1
    assert "could not be restored" in outcome.task_results[0].test_report.summary


def test_devops_artifacts_are_written_and_committed():
    responses = engine_responses()
    responses["DevOps engineer"] = json_response(
        {
            "environment": "production",
            "summary": "containerised",
            "steps": ["build image"],
            "rollback": ["previous tag"],
            "files": [
                {
                    "path": "Dockerfile",
                    "change_type": "create",
                    "summary": "app image",
                    "content": "FROM python:3.12-slim\n",
                }
            ],
        }
    )
    ws = InMemoryWorkspace()
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner, workspace=ws)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert ws.read_text("Dockerfile").startswith("FROM python")
    assert "Dockerfile" in outcome.workspace_files
    kinds = [a.kind for a in outcome.blackboard.artifacts]
    assert "deployment-artifacts" in kinds


def test_writer_docs_are_written_to_workspace():
    responses = engine_responses()
    responses["technical writer"] = json_response(
        {
            "summary": "docs",
            "sections": [{"title": "Overview", "content": "..."}],
            "files": [
                {
                    "path": "docs/login.md",
                    "change_type": "create",
                    "summary": "user docs",
                    "content": "# Login\n",
                }
            ],
        }
    )
    ws = InMemoryWorkspace({"README.md": "# App"})
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner, workspace=ws)
    run(engine.deliver(_request()))
    assert ws.read_text("docs/login.md") == "# Login\n"
    writer_prompt = next(
        c["prompt"] for c in runner.calls if "technical writer" in (c["system_prompt"] or "")
    )
    assert "README.md" in writer_prompt  # aware of existing docs
    assert "x = 1" in writer_prompt  # grounded in delivered code


def test_sre_sees_deployment_rollback_and_gates():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner)
    run(engine.deliver(_request()))
    sre_prompt = next(
        c["prompt"]
        for c in runner.calls
        if "site reliability engineer" in (c["system_prompt"] or "")
    )
    assert "rollback" in sre_prompt.lower()
    assert "revert" in sre_prompt  # deploy_dict rollback step
    assert "passed their quality gates" in sre_prompt
    assert "x = 1" in sre_prompt  # the delivered code itself


# --- review-hardening: planning resilience, WIP banking, resume ------------


def test_planning_failure_returns_halted_outcome():
    mapping = engine_responses()
    mapping["product manager"] = "utter garbage, no JSON here"
    runner = ScriptedRunner(by_system_prompt=mapping)
    engine = _engine(runner, config=EngineConfig(json_retries=0))
    outcome = run(engine.deliver(_request()))
    assert outcome.halted_reason is not None
    assert "planning failed" in outcome.halted_reason
    assert outcome.task_results == []


def test_budget_exhausted_during_planning_halts_gracefully():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, budget=Budget(limit_usd=0.0))
    outcome = run(engine.deliver(_request()))
    assert outcome.halted_reason == "budget exhausted before any task work began"


def test_duplicate_task_ids_are_renamed_not_fatal():
    dup_plan = {
        "summary": "s",
        "tasks": [
            {"id": "T1", "title": "a", "description": "", "dependencies": []},
            {"id": "T1", "title": "b", "description": "", "dependencies": []},
            {"id": "T1", "title": "c", "description": "", "dependencies": []},
        ],
    }
    mapping = engine_responses()
    mapping["product manager"] = json_response(dup_plan)
    runner = ScriptedRunner(by_system_prompt=mapping)
    engine = _engine(runner)
    outcome = run(engine.deliver(_request()))
    ids = [tr.task.id for tr in outcome.task_results]
    assert ids == ["T1", "T1-2", "T1-3"]
    assert outcome.tasks_complete is True


def test_commit_denied_by_approval_gate(tmp_path):
    from dev_team.approval import DenyAll

    cmd = DispatchCommandRunner(rev_shas=["BASE", "TIP"])
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(
        runner,
        workspace=LocalWorkspace(str(tmp_path)),
        command_runner=cmd,
        approval=DenyAll(),
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.tasks_complete is True
    assert outcome.committed is False


def _banked_first_run(tmp_path):
    """Run 1 of the WIP-banking scenario: T1 accepted, T2 review-rejected."""

    mapping = {
        "product manager": [json_response(_two_task_plan())],
        "software architect": [json_response(__design())],
        "senior software engineer": [json_response(__impl())],
        "code reviewer": [json_response(__review(True)), json_response(__review(False))],
        "quality assurance engineer": [json_response(qa_suite_dict())],
        "application security engineer": [json_response(__security())],
        "technical writer": [json_response(__docs())],
        "site reliability engineer": [json_response(__rel())],
        "DevOps engineer": [json_response(__deploy())],
    }
    ws = LocalWorkspace(str(tmp_path))
    cmd = DispatchCommandRunner(rev_shas=["BASE"])
    engine = _engine(
        KeyedQueueRunner(mapping),
        workspace=ws,
        command_runner=cmd,
        config=EngineConfig(max_task_attempts=1),
    )
    first = run(engine.deliver(_request()))
    return ws, cmd, first


def test_agentic_wip_commits_bank_each_accepted_task(tmp_path):
    _, cmd, first = _banked_first_run(tmp_path)
    assert first.success is False  # T2 was rejected
    calls = [c for c, _ in cmd.calls]
    # T1's accepted work was banked as a WIP commit on the delivery branch...
    assert ("git", "commit", "--allow-empty", "-m", "wip(dev-team): T1") in calls
    # ...so T2's rollback (hard reset) rewound to the banked state, not baseline
    assert ("git", "reset", "--hard") in calls
    # head == baseline sha (fake git repeats BASE) -> nothing squashed/committed
    assert first.committed is False


def test_resume_reuses_plan_and_original_baseline(tmp_path):
    ws, _, _ = _banked_first_run(tmp_path)
    # a file changed by run 1 (visible only via git) still reaches security
    ws.write_text("carried.py", "carried = 1")

    mapping2 = {
        "software architect": [json_response(__design())],
        "senior software engineer": [json_response(__impl())],
        "code reviewer": [json_response(__review(True))],
        "quality assurance engineer": [json_response(qa_suite_dict())],
        "application security engineer": [json_response(__security())],
        "technical writer": [json_response(__docs())],
        "site reliability engineer": [json_response(__rel())],
        "DevOps engineer": [json_response(__deploy())],
    }
    runner2 = KeyedQueueRunner(mapping2)
    # a.py is already reported by T2's implementation; carried.py is git-only
    cmd2 = DispatchCommandRunner(rev_shas=["NEW", "TIP"], diff_names=["carried.py", "a.py"])
    engine2 = _engine(runner2, workspace=ws, command_runner=cmd2)
    second = run(engine2.deliver(_request()))

    # the plan came from the checkpoint (no PM in mapping2: a call would raise)
    assert second.resumed_task_ids == ["T1"]
    assert second.success is True
    assert second.committed is True
    calls2 = [c for c, _ in cmd2.calls]
    # squashed from run 1's original baseline, not this run's fresh HEAD
    assert ("git", "reset", "--soft", "BASE") in calls2
    # run 1's carried-over change was part of the security review evidence
    assert any("carried = 1" in c["prompt"] for c in runner2.calls)


def test_worktree_merge_conflict_is_cleaned_up_and_retried(tmp_path):
    class _ConflictOnce(DispatchCommandRunner):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.merges = 0

        def run(self, command, *, cwd=None, timeout=None):
            if "merge --squash" in " ".join(command):
                self.merges += 1
                if self.merges == 1:
                    self.calls.append((tuple(command), cwd))
                    return CommandResult(list(command), 1, "", "CONFLICT (content): a.py")
            return super().run(command, cwd=cwd, timeout=timeout)

    cmd = _ConflictOnce(rev_shas=["BASE", "TIP"])
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _worktree_engine(tmp_path, runner, cmd, max_task_attempts=2)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert outcome.task_results[0].attempts == 2
    calls = [c for c, _ in cmd.calls]
    assert ("git", "reset", "--hard") in calls  # the conflicted merge was discarded
    assert any(
        "does not merge cleanly" in c["prompt"]
        for c in runner.calls
        if "Implement" in c["prompt"]
    )


def test_specialist_failure_degrades_gracefully():
    # the security agent never produces usable JSON -> stage fails, run survives
    mapping = engine_responses()
    mapping["application security engineer"] = "not json at all"
    runner = ScriptedRunner(by_system_prompt=mapping)
    engine = _engine(runner, config=EngineConfig(json_retries=0))
    outcome = run(engine.deliver(_request()))
    assert outcome.tasks_complete is True
    assert outcome.security is None  # no verdict...
    assert outcome.committed is False  # ...fails closed at commit time
    # a run with nothing vetted is never a success, however green the tasks
    assert outcome.success is False


def test_deliver_cyclic_plan_halts_gracefully():
    # A plan whose tasks form a cycle stays cyclic through the lint-revision
    # pass, reaches the scheduler, and raises DependencyCycleError. deliver()
    # must catch it: mark the un-run tasks FAILED and return a full outcome
    # (trace, cost, specialists) rather than unwind and lose everything.
    cyclic = {
        "summary": "cyclic",
        "tasks": [
            {"id": "A", "title": "a", "description": "", "acceptance_criteria": ["x"],
             "dependencies": ["B"]},
            {"id": "B", "title": "b", "description": "", "acceptance_criteria": ["y"],
             "dependencies": ["A"]},
        ],
    }
    mapping = engine_responses()
    mapping["product manager"] = json_response(cyclic)
    runner = ScriptedRunner(by_system_prompt=mapping)
    engine = _engine(runner)
    outcome = run(engine.deliver(_request()))  # does not raise
    assert outcome.success is False
    assert outcome.tasks_complete is False
    assert len(outcome.task_results) == 2
    assert all(tr.task.status is TaskStatus.FAILED for tr in outcome.task_results)
    assert outcome.committed is False


def test_backlog_reuses_epic_and_stories_across_runs():
    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    engine1 = _engine(
        ScriptedRunner(by_system_prompt=engine_responses()), workspace=ws, backlog_store=store
    )
    run(engine1.deliver(_request()))
    engine2 = _engine(
        ScriptedRunner(by_system_prompt=engine_responses()), workspace=ws, backlog_store=store
    )
    run(engine2.deliver(_request()))
    backlog = store.load()
    assert len(backlog.epics) == 1  # rerun reused the epic
    assert len(backlog.stories) == 1  # and the story
    assert backlog.stories[0].status is ItemStatus.DONE


def test_config_rejects_bad_remote_verify_settings():
    with pytest.raises(ValueError):
        EngineConfig(remote_verify_status=["ci"], remote_verify_max_polls=0)
    with pytest.raises(ValueError):
        EngineConfig(remote_verify_status=["ci"], remote_verify_interval_seconds=-1)
    with pytest.raises(ValueError):
        EngineConfig(remote_verify_trigger=["ci", "run"])


def test_remote_verify_config_builds_remote_gate():
    engine = DeliveryEngine(
        ScriptedRunner([]),
        workspace=InMemoryWorkspace(),
        command_runner=FakeCommandRunner(),
        config=EngineConfig(
            remote_verify_status=("ci", "status"),
            remote_verify_trigger=("ci", "run"),
            remote_verify_max_polls=2,
            remote_verify_interval_seconds=0.0,
        ),
    )
    assert engine._local_verification is False
    (gate,) = engine.definition_of_done.gates
    assert isinstance(gate, RemoteCIGate)
    assert list(gate.status_command) == ["ci", "status"]
    assert list(gate.trigger_command) == ["ci", "run"]
    assert gate.max_polls == 2


def test_explicit_verify_command_beats_remote_verify():
    engine = DeliveryEngine(
        ScriptedRunner([]),
        workspace=InMemoryWorkspace(),
        command_runner=FakeCommandRunner(),
        config=EngineConfig(
            verify_command=("pytest", "-q"), remote_verify_status=("ci", "status")
        ),
    )
    assert engine._local_verification is True
    (gate,) = engine.definition_of_done.gates
    assert gate.name == "tests"


def test_gates_degrade_when_project_not_locally_runnable():
    events = []
    legacy = (
        '<Project ToolsVersion="12.0">'
        "<TargetFrameworkVersion>v4.5.2</TargetFrameworkVersion></Project>"
    )
    engine = DeliveryEngine(
        ScriptedRunner([]),
        workspace=InMemoryWorkspace(
            {"App.sln": "x", "App/App.csproj": legacy, "App/packages.config": "<p/>"}
        ),
        command_runner=FakeCommandRunner(),
        listener=events.append,
    )
    engine._resolve_gates()
    assert engine._local_verification is False
    (gate,) = engine.definition_of_done.gates
    assert gate.name == "verification-unavailable"
    report = engine.definition_of_done.evaluate(engine._gate_context())
    assert report.passed is True
    assert "evidence-based review" in report.results[0].detail
    assert any("no local verify command" in e.message for e in events)
    assert engine.blackboard.get("project_profile") == "dotnet-framework"


def test_vacuous_check_skipped_without_local_verification():
    engine = DeliveryEngine(
        ScriptedRunner([]),
        workspace=InMemoryWorkspace({"x.py": "x = 1"}),
        command_runner=FakeCommandRunner(),
        config=EngineConfig(remote_verify_status=("ci", "status")),
    )
    impl = Implementation(task_id="T1", summary="s", files=[])
    assert run(engine._tests_are_vacuous(impl, engine.workspace, engine.git, None, None)) is False


def test_delivery_injects_stored_conventions_into_prompts():
    from dev_team.conventions import ConventionsProfile, ConventionsStore

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    ws = InMemoryWorkspace()
    ConventionsStore(ws).save(
        ConventionsProfile(
            summary="PascalCase members; MSTest tests.",
            conventions=[{"aspect": "naming", "convention": "PascalCase everywhere"}],
        )
    )
    engine = _engine(runner, workspace=ws)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert engine._conventions is not None

    def _prompts_for(role_fragment):
        return [
            c["prompt"]
            for c in runner.calls
            if role_fragment in (c.get("system_prompt") or "")
        ]

    assert any("House conventions" in p for p in _prompts_for("software engineer"))
    assert any("House conventions" in p for p in _prompts_for("code reviewer"))


def test_delivery_without_conventions_leaves_prompts_clean():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert engine._conventions is None
    assert not any("House conventions" in c["prompt"] for c in runner.calls)


def test_sandbox_boxes_gates_but_delegates_git_to_host(tmp_path):
    # With EngineConfig.sandbox set on a real workspace, the commands the engine
    # runs (gates/setup/scans) are containerised — except git, which
    # self-delegates to the host so porcelain still acts on the real repo.
    from dev_team.sandbox import SandboxConfig

    fake = FakeCommandRunner()
    engine = _engine(
        ScriptedRunner(by_system_prompt={}),
        workspace=LocalWorkspace(str(tmp_path)),
        command_runner=fake,
        config=EngineConfig(sandbox=SandboxConfig(image="toolchain:1")),
    )
    engine.command_runner.run(["pytest", "-q"], cwd=str(tmp_path))
    gate = fake.calls[-1]
    assert gate[:2] == ["docker", "run"]
    assert "toolchain:1" in gate
    assert gate[-2:] == ["pytest", "-q"]

    engine.command_runner.run(["git", "status"], cwd=str(tmp_path))
    assert fake.calls[-1] == ["git", "status"]


# --- dynamic re-planning loop (--max-replan-rounds) ---------------------


def _rp_engine(**kwargs):
    """A dry-run engine whose manager.replan is monkeypatched per test."""

    return _engine(ScriptedRunner([]), **kwargs)


def _done(tid):
    task = Task(id=tid, title=tid, description="", acceptance_criteria=["ok"])
    task.status = TaskStatus.DONE
    return task, TaskResult(task=task, attempts=1)


def _failed(tid, deps=None):
    task = Task(
        id=tid, title=tid, description="",
        acceptance_criteria=["ok"], dependencies=list(deps or []),
    )
    task.status = TaskStatus.FAILED
    return task, TaskResult(task=task, attempts=3)


def test_engine_config_rejects_negative_replan_rounds():
    with pytest.raises(ValueError, match="max_replan_rounds"):
        EngineConfig(max_replan_rounds=-1)


def test_failure_evidence_handles_a_missing_result():
    # Defensive: no TaskResult on record still yields a usable evidence string.
    assert _rp_engine()._failure_evidence(None) == "no evidence captured"


def test_replan_loop_off_by_default_is_a_noop():
    eng = _rp_engine()  # max_replan_rounds defaults to 0
    t2, r2 = _failed("T2")
    plan = Plan(summary="s", tasks=[t2])
    results = {"T2": r2}
    called = []

    async def replan(*a, **k):
        called.append(1)
        return Replan(ReplanAction.DROP, "T2", [])

    eng.manager.replan = replan

    async def worker(task):  # pragma: no cover - must never run
        raise AssertionError("no rescheduling when re-planning is off")

    new_plan = run(eng._replan_loop(_request(), plan, results, worker))
    assert new_plan is plan and not called


def test_replan_loop_replaces_failed_task_and_reschedules():
    eng = _rp_engine()
    eng.config.max_replan_rounds = 1
    eng._checkpoint = RunCheckpoint(feature_title="Login")  # exercises the refresh
    t1, r1 = _done("T1")
    t2, r2 = _failed("T2")
    plan = Plan(summary="s", tasks=[t1, t2])
    results = {"T1": r1, "T2": r2}

    async def replan(request, plan, task, evidence, *, revision_feedback=None):
        return Replan(
            ReplanAction.REPLACE, task.id,
            [Task(id="T2b", title="retry", description="", acceptance_criteria=["ok"])],
        )

    eng.manager.replan = replan

    async def worker(task):
        task.status = TaskStatus.DONE
        results[task.id] = TaskResult(task=task, attempts=1)
        return True

    new_plan = run(eng._replan_loop(_request(), plan, results, worker))
    assert [t.id for t in new_plan.tasks] == ["T1", "T2b"]
    assert results["T2b"].succeeded  # the replacement was scheduled and passed
    assert eng._checkpoint.plan is not None  # checkpoint refreshed to the new plan


def test_replan_loop_stops_when_nothing_failed():
    eng = _rp_engine()
    eng.config.max_replan_rounds = 2
    t1, r1 = _done("T1")
    plan = Plan(summary="s", tasks=[t1])
    results = {"T1": r1}
    called = []

    async def replan(*a, **k):
        called.append(1)
        return Replan(ReplanAction.DROP, "T1", [])

    eng.manager.replan = replan

    async def worker(task):  # pragma: no cover - nothing to schedule
        raise AssertionError("unreachable")

    new_plan = run(eng._replan_loop(_request(), plan, results, worker))
    assert new_plan is plan and not called


def test_replan_loop_leaves_task_failed_when_supervisor_rejects():
    eng = _rp_engine()
    eng.config.max_replan_rounds = 1
    eng.interaction = ScriptedChannel(script=[Reply(choice="reject")])
    t1, r1 = _done("T1")
    t2, r2 = _failed("T2")
    plan = Plan(summary="s", tasks=[t1, t2])
    results = {"T1": r1, "T2": r2}

    async def replan(request, plan, task, evidence, *, revision_feedback=None):
        return Replan(
            ReplanAction.REPLACE, task.id,
            [Task(id="T2b", title="x", description="", acceptance_criteria=["ok"])],
        )

    eng.manager.replan = replan

    async def worker(task):  # pragma: no cover - rejection means no reschedule
        raise AssertionError("a rejected re-plan must not reschedule")

    new_plan = run(eng._replan_loop(_request(), plan, results, worker))
    assert [t.id for t in new_plan.tasks] == ["T1", "T2"]  # unchanged


def test_replan_loop_discards_an_invalid_mutation():
    eng = _rp_engine()
    eng.config.max_replan_rounds = 1
    t1, r1 = _failed("T1")  # the only task
    plan = Plan(summary="s", tasks=[t1])
    results = {"T1": r1}

    async def replan(request, plan, task, evidence, *, revision_feedback=None):
        return Replan(ReplanAction.DROP, task.id, [])  # dropping the only task -> empty plan

    eng.manager.replan = replan

    async def worker(task):  # pragma: no cover - the mutation is discarded
        raise AssertionError("invalid mutation must not reschedule")

    new_plan = run(eng._replan_loop(_request(), plan, results, worker))
    assert [t.id for t in new_plan.tasks] == ["T1"]  # discarded, left as-is


def test_replan_loop_drops_a_failed_task_with_nothing_left_to_schedule():
    eng = _rp_engine()
    eng.config.max_replan_rounds = 1
    t1, r1 = _done("T1")
    t2, r2 = _failed("T2")
    plan = Plan(summary="s", tasks=[t1, t2])
    results = {"T1": r1, "T2": r2}

    async def replan(request, plan, task, evidence, *, revision_feedback=None):
        return Replan(ReplanAction.DROP, task.id, [])

    eng.manager.replan = replan

    async def worker(task):  # pragma: no cover - drop leaves nothing pending
        raise AssertionError("nothing to reschedule after a pure drop")

    new_plan = run(eng._replan_loop(_request(), plan, results, worker))
    assert [t.id for t in new_plan.tasks] == ["T1"]  # T2 dropped


def test_replan_loop_stops_the_round_when_budget_dies_mid_proposal():
    eng = _rp_engine()
    eng.config.max_replan_rounds = 1
    t1, r1 = _failed("T1")
    t2, r2 = _failed("T2")
    plan = Plan(summary="s", tasks=[t1, t2])
    results = {"T1": r1, "T2": r2}
    calls = []

    async def replan(request, plan, task, evidence, *, revision_feedback=None):
        calls.append(task.id)
        raise BudgetExceededError(10.0, 5.0)

    eng.manager.replan = replan

    async def worker(task):  # pragma: no cover - budget death means no reschedule
        raise AssertionError("no reschedule after budget exhaustion")

    new_plan = run(eng._replan_loop(_request(), plan, results, worker))
    assert [t.id for t in new_plan.tasks] == ["T1", "T2"]  # unchanged
    assert calls == ["T1"]  # broke after the first task, never tried T2


def test_propose_replan_revises_then_applies():
    eng = _rp_engine()
    eng.interaction = ScriptedChannel(
        script=[Reply(choice="revise", text="try smaller"), Reply(choice="apply")]
    )
    t2, r2 = _failed("T2")
    plan = Plan(summary="s", tasks=[t2])
    results = {"T2": r2}
    feedbacks = []

    async def replan(request, plan, task, evidence, *, revision_feedback=None):
        feedbacks.append(revision_feedback)
        return Replan(
            ReplanAction.REPLACE, task.id,
            [Task(id="T2b", title="x", description="", acceptance_criteria=["ok"])],
        )

    eng.manager.replan = replan
    decision = run(eng._propose_replan(_request(), plan, t2, results))
    assert decision is not None and decision.action is ReplanAction.REPLACE
    assert feedbacks == [None, "try smaller"]  # first proposal, then revised


def test_propose_replan_stops_on_budget_exhaustion():
    eng = _rp_engine()
    t2, r2 = _failed("T2")
    plan = Plan(summary="s", tasks=[t2])

    async def replan(*a, **k):
        raise BudgetExceededError(10.0, 5.0)

    eng.manager.replan = replan
    decision = run(eng._propose_replan(_request(), plan, t2, {"T2": r2}))
    assert decision is None
    assert eng._budget_exhausted is True
