"""Simplest possible usage — 3 lines.

Just set your API key env var (e.g. ANTHROPIC_API_KEY) and go.
"""

from aistatus import route

# Pass model name directly — provider is auto-detected
resp = route("What is the capital of France?", model="claude-sonnet-4-6")

print(resp.content)
print(f"  model:    {resp.model_used}")
print(f"  fallback: {resp.was_fallback}")
print(f"  tokens:   {resp.input_tokens} in / {resp.output_tokens} out")
