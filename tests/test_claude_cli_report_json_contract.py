# -*- coding: utf-8 -*-
"""Offline contract for report JSON returned through the ``claude_code_cli`` backend.

A mock CLI prints the same ``--output-format json`` envelope the real Claude Code CLI
prints; the reply inside it goes through the real preset extractor and the analyzer's
strict ``_validate_json_response``. Shapes mirror the real 2330.TW failure (prose, a
fenced JSON block, then a summary) plus the accepted forms and broken envelopes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.analyzer import GeminiAnalyzer  # noqa: E402
from src.llm.generation_backend import GenerationError, GenerationErrorCode  # noqa: E402
from src.llm.local_cli_backend import (  # noqa: E402
    CLAUDE_CODE_CLI_PRESET,
    LocalCliGenerationBackend,
    LocalCliPreset,
)

_REPORT = {
    "stock_name": "台積電",
    "sentiment_score": 40,
    "trend_prediction": "震荡",
    "operation_advice": "观望",
    "analysis_summary": "数据缺失，无法判断",
}
_REPORT_JSON = json.dumps(_REPORT, ensure_ascii=False, indent=2)


def _config():
    return SimpleNamespace(
        generation_backend_timeout_seconds=5,
        generation_backend_max_output_bytes=1024 * 1024,
        generation_backend_max_concurrency=1,
        local_cli_backend_max_concurrency=1,
        generation_backend="claude_code_cli",
        generation_fallback_backend="",
        report_language="zh",
    )


def _analyzer() -> GeminiAnalyzer:
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    analyzer._config_override = SimpleNamespace(generation_backend="claude_code_cli", generation_fallback_backend="")
    return analyzer


def _generate(tmp_path: Path, stdout: str):
    """Run the backend against a mock CLI that prints ``stdout`` verbatim."""
    payload_path = tmp_path / "stdout.txt"
    payload_path.write_text(stdout, encoding="utf-8")
    script = tmp_path / "mock_claude.py"
    script.write_text(
        "import pathlib, sys\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write(pathlib.Path({str(payload_path)!r}).read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    preset = LocalCliPreset(
        preset_id="claude_code_cli",
        executable=sys.executable,
        argv=(str(script), *CLAUDE_CODE_CLI_PRESET.argv),
        display_name="Mock Claude Code CLI",
        extractor=CLAUDE_CODE_CLI_PRESET.extractor,
        contract_args=CLAUDE_CODE_CLI_PRESET.contract_args,
        model_reporter=CLAUDE_CODE_CLI_PRESET.model_reporter,
    )
    backend = LocalCliGenerationBackend(_config(), preset=preset)
    return backend.generate("prompt", {}, response_validator=_analyzer()._validate_json_response)


def _envelope(result_text: str, **overrides) -> str:
    envelope = {"type": "result", "subtype": "success", "is_error": False, "result": result_text}
    envelope.update(overrides)
    return json.dumps(envelope, ensure_ascii=False)


def test_pure_json_reply_is_accepted(tmp_path: Path) -> None:
    result = _generate(tmp_path, _envelope(_REPORT_JSON))

    assert json.loads(result.text) == _REPORT
    assert result.backend == "claude_code_cli"


def test_single_fenced_json_block_alone_is_accepted(tmp_path: Path) -> None:
    result = _generate(tmp_path, _envelope(f"```json\n{_REPORT_JSON}\n```"))

    assert result.backend == "claude_code_cli"


@pytest.mark.parametrize(
    "reply",
    [
        # The real 2330.TW shape: name note, fenced JSON, then a numbered summary.
        f"台積電的正确中文全称为 **台积电**。以下是决策仪表盘：\n\n```json\n{_REPORT_JSON}\n```\n\n**重点结论速览：**\n1. 多头排列？否",
        f"以下是决策仪表盘：\n\n```json\n{_REPORT_JSON}\n```",
        f"```json\n{_REPORT_JSON}\n```\n\n补充说明：数据缺失。",
        f"说明文字\n{_REPORT_JSON}",
        f"{_REPORT_JSON}\n总结：观望",
        f"{_REPORT_JSON}\n{_REPORT_JSON}",
        f"```json\n{_REPORT_JSON}\n```\n```json\n{_REPORT_JSON}\n```",
    ],
    ids=["real_2330_shape", "prose_before_fence", "prose_after_fence", "prose_before_raw",
         "prose_after_raw", "two_objects", "two_fences"],
)
def test_json_with_extra_text_or_multiple_objects_is_rejected(tmp_path: Path, reply: str) -> None:
    with pytest.raises(GenerationError) as exc_info:
        _generate(tmp_path, _envelope(reply))

    assert exc_info.value.error_code == GenerationErrorCode.INVALID_JSON
    assert exc_info.value.details["reason"] == "ambiguous_json"
    assert exc_info.value.stage == "validation"


@pytest.mark.parametrize(
    ("stdout", "error_code", "reason"),
    [
        ("not an envelope", GenerationErrorCode.INVALID_JSON, "invalid_json"),
        ("", GenerationErrorCode.EMPTY_OUTPUT, "empty_output"),
        (json.dumps(["result"]), GenerationErrorCode.SCHEMA_VALIDATION_FAILED, "schema_validation_failed"),
        (_envelope(_REPORT_JSON, type="assistant"), GenerationErrorCode.CAPABILITY_UNSUPPORTED, "unexpected_cli_event"),
        (_envelope("usage limit reached", is_error=True), GenerationErrorCode.UNKNOWN_BACKEND_ERROR, "cli_result_error"),
        (_envelope(_REPORT_JSON, subtype="error_during_execution"), GenerationErrorCode.UNKNOWN_BACKEND_ERROR,
         "cli_result_not_success"),
        (_envelope(""), GenerationErrorCode.EMPTY_OUTPUT, "empty_result"),
    ],
    ids=["not_json", "empty_stdout", "non_object", "wrong_event", "is_error", "not_success", "empty_result"],
)
def test_broken_envelope_fails_explicitly(tmp_path: Path, stdout: str, error_code, reason: str) -> None:
    with pytest.raises(GenerationError) as exc_info:
        _generate(tmp_path, stdout)

    assert exc_info.value.error_code == error_code
    assert exc_info.value.details["reason"] == reason
    assert exc_info.value.backend == "claude_code_cli"


@pytest.mark.parametrize("report_language", ["zh", "en"])
def test_analysis_prompt_asks_for_json_only_and_no_prose(report_language: str) -> None:
    with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
        analyzer = GeminiAnalyzer()
    context = {"code": "2330.TW", "stock_name": "台積電", "date": "2026-11-06", "today": {}}

    prompt = analyzer._format_prompt(context, "台積電", news_context=None, report_language=report_language)

    assert "在分析开头" not in prompt
    assert "at the beginning of the analysis" not in prompt
    assert "`stock_name`" in prompt
    assert "`action_checklist`" in prompt
    if report_language == "zh":
        assert "只输出一个 JSON 对象：JSON 之前和之后不得有任何说明、标题或总结，也不要使用 Markdown 代码块。" in prompt
    else:
        assert "Output exactly one JSON object and nothing else" in prompt


# ---------------------------------------------------------------- reported model vs backend
def test_envelope_model_usage_is_reported_as_the_model(tmp_path: Path) -> None:
    stdout = _envelope(_REPORT_JSON, modelUsage={
        "claude-haiku-test": {"inputTokens": 900, "outputTokens": 12},
        "claude-sonnet-test": {"inputTokens": 4000, "outputTokens": 1800},
    })

    result = _generate(tmp_path, stdout)

    assert result.model == "claude-sonnet-test"
    assert result.backend == "claude_code_cli"
    assert result.diagnostics["reported_model"] == "claude-sonnet-test"


def test_envelope_without_model_leaves_the_model_unreported(tmp_path: Path) -> None:
    result = _generate(tmp_path, _envelope(_REPORT_JSON))

    assert result.diagnostics["reported_model"] is None
    # The preset id is the "unknown" marker the analyzer turns into model_used=None.
    assert result.model == "claude_code_cli"


@pytest.mark.parametrize(
    ("model_used", "expected"),
    [(None, "未知（分析后端 claude_code_cli）"), ("claude-sonnet-test", "claude-sonnet-test（分析后端 claude_code_cli）")],
)
def test_report_footer_separates_backend_from_model(model_used, expected) -> None:
    from src.notification import NotificationService
    from src.report_language import get_report_labels

    result = SimpleNamespace(model_used=model_used, generation_backend="claude_code_cli", report_language="zh")

    assert NotificationService._model_display(result, get_report_labels("zh")) == expected


def test_litellm_model_display_is_unchanged() -> None:
    from src.notification import NotificationService
    from src.report_language import get_report_labels

    result = SimpleNamespace(model_used="gemini/gemini-2.5-pro", generation_backend=None)

    assert NotificationService._model_display(result, get_report_labels("zh")) == "gemini/gemini-2.5-pro"
