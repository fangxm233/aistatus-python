# input: OpenAI Responses API streams, including ones mislabelled as application/json
# output: regression coverage for Codex usage accounting and content-type body probing
# pos: gateway OpenAI Responses API accounting test suite (mirrors TS tests/codex-responses-usage.test.mjs)
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<
"""Tests for the OpenAI Responses API (`/v1/responses`, the ChatGPT Codex backend).

Two things are specific to this protocol. Usage arrives nested under `response` on a terminal
event, and its `input_tokens` is a TOTAL that already includes the cached prefix — unlike Anthropic,
where `input_tokens` excludes it. And the real backend labels its event streams `application/json`,
so the content-type header cannot be trusted to decide whether to stream.
"""

from __future__ import annotations

import json

import pytest

from tests.gateway_harness import proxy_once

ANTHROPIC_SSE = (
    'data: {"type":"message_start","message":{"model":"claude-opus-5",'
    '"usage":{"input_tokens":10,"cache_read_input_tokens":400}}}\n\n'
    'data: {"type":"message_delta","usage":{"output_tokens":25}}\n\n'
    "data: [DONE]\n\n"
)


def responses_sse(usage: dict, model: str = "gpt-5.6-sol") -> str:
    """A realistic Codex stream: deltas carry no usage, only the terminal event reports totals."""
    events = [
        {"type": "response.created", "response": {"id": "r1", "model": model, "usage": None}},
        {"type": "response.output_text.delta", "delta": "hi", "output_index": 0},
        {"type": "response.completed",
         "response": {"id": "r1", "model": model, "status": "completed", "usage": usage}},
    ]
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


async def _codex(tmp_path, body, **kwargs):
    return await proxy_once(
        tmp_path, body, endpoint_name="openai", path="codex/responses",
        request_body=b'{"model":"gpt-5.6-sol","input":[],"stream":true}', **kwargs,
    )


class TestResponsesUsage:
    @pytest.mark.asyncio
    async def test_splits_cached_input_out_of_the_total(self, tmp_path):
        result = await _codex(tmp_path, responses_sse({
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 800},
            "output_tokens": 50,
            "output_tokens_details": {"reasoning_tokens": 20},
            "total_tokens": 1050,
        }))

        assert len(result.records) == 1
        record = result.records[0]
        assert record["model"] == "gpt-5.6-sol"
        # `openai-codex` folds down to the `openai` vendor, the way any `<vendor>-<variant>`
        # endpoint does. The model name distinguishes Codex traffic; the vendor keys pricing.
        assert record["provider"] == "openai"
        # Recording `input_tokens` verbatim would double-count the 800 cached tokens.
        assert record["in"] == 200
        assert record["cache_read_in"] == 800
        assert record["out"] == 50
        assert record["project"] == "demo"

    @pytest.mark.asyncio
    async def test_counts_cache_writes_separately(self, tmp_path):
        result = await _codex(tmp_path, responses_sse({
            "input_tokens": 120,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 20},
            "output_tokens": 7,
        }))

        assert result.records[0]["in"] == 100
        assert result.records[0]["cache_creation_in"] == 20
        assert result.records[0]["out"] == 7

    @pytest.mark.asyncio
    async def test_names_the_model_from_the_stream_when_the_request_is_unreadable(self, tmp_path):
        # PI zstd-compresses the Codex request body, so the model cannot be read from it. Without a
        # name recovered from the stream the row would land as `openai/unknown`.
        result = await proxy_once(
            tmp_path,
            responses_sse({"input_tokens": 30, "input_tokens_details": {"cached_tokens": 0},
                           "output_tokens": 4}, model="gpt-6-astra"),
            endpoint_name="openai", path="codex/responses",
            request_body=bytes([0x28, 0xB5, 0x2F, 0xFD, 0x00, 0x58, 0x99, 0x00, 0x00]),
        )

        assert len(result.records) == 1
        assert result.records[0]["model"] == "gpt-6-astra"
        assert result.records[0]["in"] == 30

    @pytest.mark.asyncio
    async def test_leaves_anthropic_usage_untouched(self, tmp_path):
        # Anthropic carries no `input_tokens_details`, so it must not be re-normalized: its
        # `input_tokens` already excludes the cached prefix, and subtracting again would zero it.
        result = await proxy_once(tmp_path, ANTHROPIC_SSE)

        assert len(result.records) == 1
        assert result.records[0]["in"] == 10
        assert result.records[0]["cache_read_in"] == 400
        assert result.records[0]["out"] == 25


class TestContentTypeProbe:
    @pytest.mark.asyncio
    async def test_streams_an_event_stream_mislabelled_as_json(self, tmp_path):
        # What the real ChatGPT backend does. Trusting the header buffers the whole stream and
        # hands it to the JSON usage parser — exactly how Codex usage went unrecorded.
        body = responses_sse({"input_tokens": 18,
                              "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                              "output_tokens": 5})
        result = await _codex(tmp_path, body, content_type="application/json")

        assert len(result.records) == 1
        assert result.records[0]["in"] == 18
        assert result.records[0]["out"] == 5
        # The client is told the truth about what it is receiving.
        assert result.headers["Content-Type"].startswith("text/event-stream")
        assert result.body == body

    @pytest.mark.asyncio
    async def test_still_buffers_a_genuine_json_response(self, tmp_path):
        body = json.dumps({
            "id": "r1", "model": "gpt-5.6-sol",
            "usage": {"input_tokens": 30, "input_tokens_details": {"cached_tokens": 10},
                      "output_tokens": 4},
        })
        result = await _codex(tmp_path, body, content_type="application/json")

        assert result.headers["Content-Type"].startswith("application/json")
        assert len(result.records) == 1
        assert result.records[0]["in"] == 20
        assert result.records[0]["cache_read_in"] == 10
        assert result.records[0]["out"] == 4
        assert json.loads(result.body)["id"] == "r1"

    @pytest.mark.asyncio
    async def test_probe_does_not_misread_json_starting_with_a_data_key(self, tmp_path):
        # `{"data": ...}` is a common JSON shape and must not be mistaken for an SSE `data:` line.
        body = json.dumps({"data": [1, 2, 3], "model": "gpt-5.6-sol",
                           "usage": {"prompt_tokens": 5, "completion_tokens": 1}})
        result = await _codex(tmp_path, body, content_type="application/json")

        assert result.headers["Content-Type"].startswith("application/json")
        assert result.records[0]["in"] == 5
