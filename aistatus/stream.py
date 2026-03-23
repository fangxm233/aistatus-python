"""Stream helpers for converting async generators of StreamChunk."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from .models import StreamChunk


async def collect_stream_text(stream: AsyncGenerator[StreamChunk, None]) -> str:
    """Collect all text chunks from a stream into a single string."""
    parts: list[str] = []
    async for chunk in stream:
        if chunk.get("type") == "text" and "text" in chunk:
            parts.append(chunk["text"])  # type: ignore[typeddict-item]
        elif chunk.get("type") == "error":
            error = chunk.get("error")  # type: ignore[typeddict-item]
            if error:
                raise error
    return "".join(parts)


async def stream_to_text_chunks(
    stream: AsyncGenerator[StreamChunk, None],
) -> AsyncGenerator[str, None]:
    """Yield only text strings from a StreamChunk async generator."""
    async for chunk in stream:
        if chunk.get("type") == "text" and "text" in chunk:
            yield chunk["text"]  # type: ignore[typeddict-item]
        elif chunk.get("type") == "error":
            error = chunk.get("error")  # type: ignore[typeddict-item]
            if error:
                raise error
        elif chunk.get("type") == "done":
            return
