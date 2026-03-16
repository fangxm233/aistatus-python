"""Tier-based routing — configure once, use everywhere.

Tiers map a name to an ordered list of models to try.
Unlike model= (zero-config), tiers require explicit setup.
"""

from aistatus import Router

# Create router (auto-discovers providers from env vars)
router = Router()

# Configure tiers — you control which models and in what order
router.add_tier("fast", [
    "claude-haiku-4-5",
    "gpt-4o-mini",
    "gemini-2.0-flash",
])
router.add_tier("standard", [
    "claude-sonnet-4-6",
    "gpt-4o",
    "gemini-2.5-pro",
])
router.add_tier("flagship", [
    "claude-opus-4-6",
    "o3",
    "gemini-2.5-pro",
])

# Use tiers — if a model's provider is down, tries the next model in the tier
resp = router.route("Explain quantum computing in one sentence.", tier="fast")
print(resp.content)
print(f"  model:    {resp.model_used}")
print(f"  fallback: {resp.was_fallback}")
