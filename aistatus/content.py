"""Content block helpers for extracting and normalizing message content."""

from __future__ import annotations

from .models import ContentBlock


def extract_text_from_content(content: str | list[ContentBlock]) -> str:
    """Extract plain text from a content value (string or ContentBlock list)."""
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        if block.get("type") == "text" and "text" in block:
            parts.append(block["text"])  # type: ignore[typeddict-item]
    return "\n".join(parts)


def normalize_content(content: str | list[ContentBlock]) -> list[ContentBlock]:
    """Normalize content to a list of ContentBlock."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]  # type: ignore[return-value]
    return content
