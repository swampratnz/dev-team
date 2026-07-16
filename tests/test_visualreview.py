"""Tests for the visual-review seams and their in-memory fakes."""

from __future__ import annotations

import base64

import pytest
from helpers import run

from dev_team.budget import Budget, BudgetExceededError
from dev_team.models import Severity
from dev_team.visualreview import (
    VISUAL_RUBRIC,
    AnthropicVisualReviewer,
    AppServer,
    FakeAppServer,
    FakePageCapturer,
    FakeVisualReviewer,
    PageCapturer,
    PlaywrightPageCapturer,
    Screenshot,
    SubprocessAppServer,
    VisualFinding,
    VisualReport,
    VisualReviewer,
    _build_content,
    _child_env,
    _join_url,
    _parse_json_object,
    _payload_from_response,
    _render_command,
    _report_from_payload,
    _severity,
    _usage_cost,
)


def test_visual_report_clean_is_true_without_major_findings():
    report = VisualReport(
        findings=[VisualFinding(route="/", issue="tiny", severity=Severity.MINOR)]
    )
    assert report.clean is True


def test_visual_report_clean_is_false_with_a_major_finding():
    report = VisualReport(
        findings=[VisualFinding(route="/", issue="broken", severity=Severity.MAJOR)]
    )
    assert report.clean is False


def test_fake_app_server_yields_base_url_and_counts_starts():
    server = FakeAppServer(base_url="http://x")
    with server.serve() as base_url:
        assert base_url == "http://x"
    assert server.starts == 1


def test_fake_page_capturer_defaults_to_one_screenshot_per_route():
    capturer = FakePageCapturer()
    shots = capturer.capture("http://x", ["/", "/steps"])
    assert [s.route for s in shots] == ["/", "/steps"]
    assert all(s.png for s in shots)
    assert capturer.calls == [("http://x", ("/", "/steps"))]


def test_fake_page_capturer_returns_provided_screenshots():
    canned = [Screenshot(route="/only", png=b"x")]
    capturer = FakePageCapturer(screenshots=canned)
    assert capturer.capture("http://x", ["/", "/steps"]) == canned


def test_fake_visual_reviewer_default_report_is_clean():
    reviewer = FakeVisualReviewer()
    shots = [Screenshot(route="/", png=b"x")]
    report = run(reviewer.critique(shots, VISUAL_RUBRIC))
    assert report.findings == []
    assert report.routes == ["/"]
    assert reviewer.seen == shots


def test_fake_visual_reviewer_returns_provided_report():
    canned = VisualReport(summary="scripted")
    reviewer = FakeVisualReviewer(report=canned)
    got = run(reviewer.critique([Screenshot(route="/", png=b"x")], VISUAL_RUBRIC))
    assert got is canned


def test_fakes_satisfy_the_protocols():
    assert isinstance(FakeAppServer(), AppServer)
    assert isinstance(FakePageCapturer(), PageCapturer)
    assert isinstance(FakeVisualReviewer(), VisualReviewer)


def test_visual_rubric_is_nonempty_guidance():
    assert "screenshot" in VISUAL_RUBRIC.lower()


# --- pure helpers -----------------------------------------------------------


def test_render_command_substitutes_port_in_each_token():
    assert _render_command(["serve", "--port", "{port}"], 8080) == [
        "serve",
        "--port",
        "8080",
    ]


def test_render_command_substitutes_embedded_and_repeated_placeholders():
    assert _render_command(["run", "--u=http://h:{port}/{port}"], 9) == [
        "run",
        "--u=http://h:9/9",
    ]


def test_render_command_leaves_tokens_without_placeholder():
    assert _render_command(["npm", "run", "preview"], 3000) == ["npm", "run", "preview"]


def test_join_url_joins_relative_routes_regardless_of_slashes():
    assert _join_url("http://h:8/", "/steps") == "http://h:8/steps"
    assert _join_url("http://h:8", "steps") == "http://h:8/steps"


def test_join_url_root_route_yields_base_slash():
    assert _join_url("http://h:8", "/") == "http://h:8/"


def test_join_url_absolute_route_is_returned_unchanged():
    assert _join_url("http://h:8", "https://cdn/x") == "https://cdn/x"


def test_usage_cost_prices_a_known_model():
    assert _usage_cost(1_000_000, 0, "claude-opus-4-8") == 5.0
    assert _usage_cost(0, 1_000_000, "claude-opus-4-8") == 25.0


def test_usage_cost_defaults_unknown_model_to_opus_tier():
    assert _usage_cost(1_000_000, 0, "mystery-model") == 5.0


def test_usage_cost_clamps_negative_tokens_to_zero():
    assert _usage_cost(-5, -5, "claude-opus-4-8") == 0.0


def test_severity_maps_known_names_case_insensitively():
    assert _severity("MAJOR") is Severity.MAJOR
    assert _severity("critical") is Severity.CRITICAL


def test_severity_defaults_to_minor_for_unknown_or_nonstring():
    assert _severity("banana") is Severity.MINOR
    assert _severity(None) is Severity.MINOR


def test_build_content_leads_with_rubric_then_one_image_per_screenshot():
    shots = [Screenshot(route="/", png=b"AB"), Screenshot(route="/x", png=b"CD")]
    content = _build_content(shots, "RUBRIC")
    assert content[0]["type"] == "text" and "RUBRIC" in content[0]["text"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 2
    assert images[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(images[0]["source"]["data"]) == b"AB"
    labels = [b["text"] for b in content if b["type"] == "text"]
    assert "Route: /x" in labels


def test_parse_json_object_reads_a_plain_object():
    assert _parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_json_object_reads_through_a_fence_and_prose():
    text = 'Here you go:\n```json\n{"a": 1}\n```\nthanks'
    assert _parse_json_object(text) == {"a": 1}


def test_parse_json_object_returns_empty_when_no_object_present():
    assert _parse_json_object("no json here") == {}


def test_parse_json_object_returns_empty_on_unclosed_object():
    assert _parse_json_object("{ no close") == {}


def test_parse_json_object_returns_empty_on_malformed_object():
    assert _parse_json_object("{not: valid}") == {}


def test_report_from_payload_maps_findings_and_severities():
    payload = {
        "summary": "s",
        "findings": [
            {"route": "/", "issue": "a", "severity": "critical"},
            {"route": "/x", "issue": "b"},  # severity omitted -> minor
        ],
    }
    report = _report_from_payload(payload, ["/", "/x"])
    assert report.summary == "s"
    assert report.routes == ["/", "/x"]
    assert [f.severity for f in report.findings] == [Severity.CRITICAL, Severity.MINOR]


def test_report_from_payload_skips_nondict_and_issueless_items():
    payload = {"findings": ["nope", {"route": "/", "issue": ""}, {"route": "/", "issue": "real"}]}
    report = _report_from_payload(payload, ["/"])
    assert [f.issue for f in report.findings] == ["real"]


def test_report_from_payload_treats_nonlist_findings_as_clean():
    report = _report_from_payload({"findings": "oops", "summary": "x"}, ["/"])
    assert report.findings == [] and report.summary == "x"


def test_report_from_payload_empty_payload_is_clean():
    report = _report_from_payload({}, ["/a"])
    assert report.findings == []
    assert report.summary == ""
    assert report.routes == ["/a"]


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResponse:
    def __init__(self, text: str, usage: _FakeUsage) -> None:
        self.content = [_FakeBlock(text)]
        self.usage = usage


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeAnthropic:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


def test_payload_from_response_ignores_blocks_without_text():
    class _NoText:
        pass

    class _Resp:
        content = [_NoText(), _FakeBlock('{"summary": "ok"}')]

    assert _payload_from_response(_Resp()) == {"summary": "ok"}


# --- real adapters: construction guards & protocol conformance --------------


def test_child_env_strips_every_orchestrator_secret():
    from dev_team.execution import SECRET_ENV_KEYS

    environ = {"PATH": "/usr/bin", "HOME": "/home/u"}
    for key in SECRET_ENV_KEYS:
        environ[key] = "leak"
    env = _child_env(environ, None)
    for key in SECRET_ENV_KEYS:
        assert key not in env  # the served (untrusted) app never sees a secret
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/u"


def test_child_env_overrides_win_over_scrubbed_base():
    environ = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "orchestrator-key"}
    env = _child_env(environ, {"APP_FLAG": "1", "ANTHROPIC_API_KEY": "scoped"})
    assert env["APP_FLAG"] == "1"
    # a deliberately-supplied scoped credential still wins over the scrub
    assert env["ANTHROPIC_API_KEY"] == "scoped"
    assert env["PATH"] == "/usr/bin"


def test_child_env_copies_rather_than_mutating_the_source():
    environ = {"ANTHROPIC_API_KEY": "secret", "PATH": "/x"}
    _child_env(environ, None)
    assert environ["ANTHROPIC_API_KEY"] == "secret"  # os.environ stays intact


def test_subprocess_app_server_requires_a_port_placeholder():
    with pytest.raises(ValueError, match="port"):
        SubprocessAppServer(serve_command=["npm", "run", "preview"])


def test_subprocess_app_server_accepts_a_valid_command():
    server = SubprocessAppServer(
        serve_command=["npm", "run", "preview", "--", "--port", "{port}"]
    )
    assert isinstance(server, AppServer)


def test_subprocess_app_server_rejects_a_policy_denied_command():
    with pytest.raises(ValueError, match="policy"):
        SubprocessAppServer(serve_command=["rm", "-rf", "/", "{port}"])


def test_real_adapters_satisfy_their_protocols():
    assert isinstance(
        SubprocessAppServer(serve_command=["s", "{port}"]), AppServer
    )
    assert isinstance(PlaywrightPageCapturer(), PageCapturer)
    assert isinstance(AnthropicVisualReviewer(), VisualReviewer)


# --- AnthropicVisualReviewer.critique (fake client, no network) -------------


def test_anthropic_reviewer_maps_response_and_sends_multimodal_content():
    payload = (
        '{"summary": "one big issue", "findings": '
        '[{"route": "/", "issue": "unstyled body", "severity": "major"}]}'
    )
    client = _FakeAnthropic(_FakeResponse(payload, _FakeUsage(10, 20)))
    reviewer = AnthropicVisualReviewer(client=client)
    report = run(reviewer.critique([Screenshot(route="/", png=b"x")], VISUAL_RUBRIC))
    assert report.summary == "one big issue"
    assert report.routes == ["/"]
    assert [f.severity for f in report.findings] == [Severity.MAJOR]
    assert report.clean is False
    sent = client.messages.calls[0]
    assert sent["model"] == "claude-opus-4-8"
    assert sent["messages"][0]["content"][0]["type"] == "text"


def test_anthropic_reviewer_meters_cost_into_the_budget():
    client = _FakeAnthropic(
        _FakeResponse('{"summary": "clean", "findings": []}', _FakeUsage(1_000_000, 0))
    )
    budget = Budget(limit_usd=100.0)
    reviewer = AnthropicVisualReviewer(client=client, budget=budget)
    report = run(reviewer.critique([Screenshot(route="/", png=b"x")], VISUAL_RUBRIC))
    assert report.findings == []
    # 1M input tokens at the opus tier ($5/1M) -> $5, attributed to "visual".
    assert budget.spent == 5.0
    assert budget.meter.cost_by_role()["visual"] == 5.0


def test_anthropic_reviewer_without_a_budget_does_not_meter():
    client = _FakeAnthropic(_FakeResponse('{"summary": "", "findings": []}', _FakeUsage(9, 9)))
    reviewer = AnthropicVisualReviewer(client=client)
    report = run(reviewer.critique([Screenshot(route="/", png=b"x")], VISUAL_RUBRIC))
    assert report.summary == ""


def test_anthropic_reviewer_refuses_when_the_budget_is_exhausted():
    client = _FakeAnthropic(_FakeResponse("{}", _FakeUsage(0, 0)))
    budget = Budget(limit_usd=0.0)  # spent 0 >= effective ceiling 0 -> exhausted
    reviewer = AnthropicVisualReviewer(client=client, budget=budget)
    with pytest.raises(BudgetExceededError):
        run(reviewer.critique([Screenshot(route="/", png=b"x")], VISUAL_RUBRIC))
    assert client.messages.calls == []  # short-circuited before the model call
