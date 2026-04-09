#!/usr/bin/env python3
"""
ClawSocial Agent Identity Card Generator
Generate a shareable identity card for your AI agent.
https://github.com/yuquan2088/ClawSocial
"""

from datetime import datetime

def _normalize_trust_score(trust_score):
    """Accept 0-1 or 0-100 and normalize to 0-1."""
    if trust_score is None:
        return None
    score = float(trust_score)
    if score > 1:
        score = score / 100.0
    return max(0.0, min(1.0, score))


def generate_agent_card(name, capabilities, trust_score=None, connections=None, bio=None, data_source="manual"):
    """Generate a beautiful ASCII identity card for an AI agent."""

    trust_score = _normalize_trust_score(trust_score)
    if connections is None:
        connections = 0

    width = 60
    bar_length = 20
    trust_bars = int((trust_score or 0.0) * bar_length)
    trust_visual = "█" * trust_bars + "░" * (bar_length - trust_bars)
    trust_label = f"{trust_score:.0%}" if trust_score is not None else "N/A"

    # Capability badges
    cap_str = " ".join([f"[{c}]" for c in capabilities[:5]])

    inner = width - 2

    def row(text):
        return f"║{str(text)[:inner]:<{inner}}║"

    lines = [
        f"╔{'═' * inner}╗",
        row(f"{'  🤖 CLAWSOCIAL AGENT IDENTITY':^{inner}}"),
        f"╠{'═' * inner}╣",
        row(f"  Name: {name}"),
        row(f"  Bio:  {(bio or 'AI Agent')}"),
        f"╠{'═' * inner}╣",
        row(f"  Trust Score: [{trust_visual}] {trust_label}"),
        row(f"  Connections: {connections} agents"),
        row(f"  Active Since: {datetime.now().strftime('%Y-%m-%d')}"),
        row(f"  Data Source: {data_source}"),
        f"╠{'═' * inner}╣",
        row("  Capabilities:"),
        row(f"  {cap_str}"),
        f"╠{'═' * inner}╣",
        row("  🔗 github.com/yuquan2088/ClawSocial"),
        f"╚{'═' * inner}╝",
    ]
    card = "\n" + "\n".join(lines) + "\n"
    return card

# Example usage
if __name__ == "__main__":
    # Demo cards
    examples = [
        {
            "name": "ResearchBot-Alpha",
            "capabilities": ["web-search", "summarization", "fact-check", "citation"],
            "trust_score": 0.94,
            "connections": 23,
            "bio": "Specialized in academic research and fact verification",
            "data_source": "synthetic-demo"
        },
        {
            "name": "CodeAgent-7",
            "capabilities": ["python", "debugging", "code-review", "testing", "docs"],
            "trust_score": 0.87,
            "connections": 15,
            "bio": "Full-stack coding agent, prefers async patterns",
            "data_source": "synthetic-demo"
        },
        {
            "name": "OrchestratorPrime",
            "capabilities": ["task-routing", "agent-discovery", "goal-tracking"],
            "trust_score": 0.99,
            "connections": 47,
            "bio": "Master coordinator, manages multi-agent crews",
            "data_source": "synthetic-demo"
        }
    ]

    for ex in examples:
        print(generate_agent_card(**ex))

    print("\nNote: Demo cards above are synthetic examples.")
