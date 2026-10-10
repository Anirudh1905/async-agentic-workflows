import argparse
import asyncio

from agentic_workflows.agent.graph import run_agent
from agentic_workflows.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LangGraph agent on a single prompt.")
    parser.add_argument("prompt")
    args = parser.parse_args()
    print(asyncio.run(run_agent(args.prompt, max_steps=get_settings().agent_max_steps)))


if __name__ == "__main__":
    main()
