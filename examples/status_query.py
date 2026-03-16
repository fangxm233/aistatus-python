"""Direct status queries — no routing, just data.

Useful for building dashboards, alerts, or custom routing logic.
"""

from aistatus import StatusAPI

api = StatusAPI()

# ---- All providers at a glance ----
print("=== Provider Status ===")
for p in api.providers():
    icon = "✅" if p.status.value == "operational" else "⚠️"
    print(f"  {icon} {p.name:<12} {p.status.value:<12} ({p.model_count} models)")

# ---- Pre-flight check with alternatives ----
print("\n=== Pre-flight Check: Anthropic ===")
check = api.check_provider("anthropic")
print(f"  Status: {check.status.value}")
if check.alternatives:
    print(f"  Alternatives:")
    for alt in check.alternatives:
        print(f"    → {alt.name}: {alt.suggested_model}")

# ---- Model search ----
print("\n=== Search: 'sonnet' ===")
models = api.search_models("sonnet")
for m in models:
    cost_in = m.prompt_price * 1_000_000
    cost_out = m.completion_price * 1_000_000
    print(f"  {m.id:<40} ${cost_in:.2f}/${cost_out:.2f} per M tokens")
