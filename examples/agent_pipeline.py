"""Agent-style pipeline: multiple LLM calls, each status-aware.

Demonstrates how an agent can use different models for different steps
and never worry about provider availability.
"""

from aistatus import route


def research_agent(topic: str) -> str:
    """A simple 3-step agent that uses different models per step."""

    # Step 1: Fast model for query decomposition
    plan = route(
        topic,
        model="claude-haiku-4-5",
        system="Break the topic into 3 research sub-questions. Be concise.",
    )
    print(f"[Plan] via {plan.model_used} (fallback={plan.was_fallback})")

    # Step 2: Standard model for each sub-question
    findings = []
    for i, question in enumerate(plan.content.strip().split("\n")[:3]):
        answer = route(
            question,
            model="claude-sonnet-4-6",
            system="Answer this research question in 2-3 sentences.",
            prefer=["anthropic", "google"],
        )
        print(f"[Research {i+1}] via {answer.model_used}")
        findings.append(answer.content)

    # Step 3: Flagship model for synthesis
    synthesis = route(
        "\n\n".join(findings),
        model="claude-opus-4-6",
        system="Synthesize these research findings into a clear summary.",
    )
    print(f"[Synthesis] via {synthesis.model_used}")

    return synthesis.content


if __name__ == "__main__":
    result = research_agent("How is embodied AI changing manufacturing?")
    print("\n" + "=" * 60)
    print(result)
