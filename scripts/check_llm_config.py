"""Validate research-agent LLM routing without making external API calls."""

from __future__ import annotations

import argparse
from pathlib import Path

from quant_system.agents.config import load_agent_llm_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="configs/agents/default.yaml",
        help="Path to agent LLM routing configuration",
    )
    args = parser.parse_args()
    config = load_agent_llm_config(Path(args.config))
    credential_status = config.credential_status()

    print("Agent role routing:")
    for role, role_config in config.roles.items():
        provider = config.providers[role_config.provider]
        configured = credential_status[role_config.provider]
        marker = "configured" if configured else f"missing {provider.api_key_env}"
        print(f"- {role}: {role_config.provider}/{role_config.model} [{marker}]")
    print("No external API request was made.")


if __name__ == "__main__":
    main()