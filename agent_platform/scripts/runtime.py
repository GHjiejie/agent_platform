"""Launch one runtime role with a filtered environment, without initialization."""

import argparse
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from agent_platform.scripts.gateway import platform_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "role", choices=["serve", "worker", "gateway-sync", "maintenance"]
    )
    parser.add_argument("--env-file", type=Path, default=Path("agent_platform/.env"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    source = {
        **{k: v for k, v in dotenv_values(args.env_file).items() if v is not None},
        **os.environ,
    }
    env = platform_environment(source)
    env["PLATFORM_EMBEDDED_WORKER"] = "false"
    if args.role == "gateway-sync":
        env["PLATFORM_GATEWAY_CONTROL_URL"] = source.get(
            "PLATFORM_GATEWAY_CONTROL_URL", ""
        )
        env["PLATFORM_GATEWAY_CONTROL_KEY"] = source.get(
            "PLATFORM_GATEWAY_CONTROL_KEY", ""
        )
    command = [sys.executable, "-m", "agent_platform", args.role]
    if args.role == "serve":
        command += ["--host", args.host, "--port", str(args.port)]
    os.execve(sys.executable, command, env)


if __name__ == "__main__":
    main()
