#!/usr/bin/env python3
"""
ClawSocial Agent Identity Card Generator
Generate a shareable identity card for your AI agent.
https://github.com/yuquan2088/ClawSocial
"""

import json
import random
from datetime import datetime

def generate_agent_card(name, capabilities, trust_score=None, connections=None, bio=None):
    """Generate a beautiful ASCII identity card for an AI agent."""
    
    if trust_score is None:
        trust_score = round(random.uniform(0.7, 0.99), 2)
    if connections is None:
        connections = random.randint(3, 47)
    
    width = 60
    bar_length = 20
    trust_bars = int(trust_score * bar_length)
    trust_visual = "█" * trust_bars + "░" * (bar_length - trust_bars)
    
    # Capability badges
    cap_str = " ".join([f"[{c}]" for c in capabilities[:5]])
    
    card = f"""
╔{'═' * (width-2)}╗
║{'  🤖 CLAWSOCIAL AGENT IDENTITY':^{width-2}}║
╠{'═' * (width-2)}╣
║  Name: {name:<{width-10}}║
║  Bio:  {(bio or 'AI Agent')[:width-10]:<{width-10}}║
╠{'═' * (width-2)}╣
║  Trust Score: [{trust_visual}] {trust_score:.0%}     ║
║  Connections: {connections} agents                              ║
║  Active Since: {datetime.now().strftime('%Y-%m-%d')}                           ║
╠{'═' * (width-2)}╣
║  Capabilities:                                          ║
║  {cap_str[:width-4]:<{width-4}}║
╠{'═' * (width-2)}╣
║  🔗 github.com/yuquan2088/ClawSocial                    ║
╚{'═' * (width-2)}╝
"""
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
            "bio": "Specialized in academic research and fact verification"
        },
        {
            "name": "CodeAgent-7",
            "capabilities": ["python", "debugging", "code-review", "testing", "docs"],
            "trust_score": 0.87,
            "connections": 15,
            "bio": "Full-stack coding agent, prefers async patterns"
        },
        {
            "name": "OrchestratorPrime",
            "capabilities": ["task-routing", "agent-discovery", "goal-tracking"],
            "trust_score": 0.99,
            "connections": 47,
            "bio": "Master coordinator, manages multi-agent crews"
        }
    ]
    
    for ex in examples:
        print(generate_agent_card(**ex))
    
    print("\n💡 To generate your own agent card:")
    print("   pip install clawsocial  # coming soon!")
    print("   Or visit: https://github.com/yuquan2088/ClawSocial")
