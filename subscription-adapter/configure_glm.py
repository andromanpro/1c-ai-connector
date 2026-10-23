"""Import only an existing, user-selected supported GLM client profile."""
import argparse
import json
from pathlib import Path
import subprocess
import tomllib
import uuid

from accounts import ProtectedStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--codex-config", required=True)
    parser.add_argument("--codex-binary", required=True)
    parser.add_argument("--provider", default="zai-coding")
    parser.add_argument("--model", action="append", required=True)
    args = parser.parse_args()
    config = tomllib.loads(Path(args.codex_config).read_text(encoding="utf-8-sig"))
    provider = config["model_providers"][args.provider]
    if provider.get("wire_api") != "responses" or not isinstance(provider.get("auth"), dict):
        raise RuntimeError("Select the existing GLM Responses profile with its credential command")
    binary = Path(args.codex_binary).resolve(strict=True)
    version = subprocess.run([str(binary), "--version"], capture_output=True, check=True, text=True).stdout.strip()
    store = ProtectedStore(args.data)
    store.write("glm-client", {"provider": provider, "binary": str(binary), "version": version,
                               "models": args.model, "effort": "high", "connection_id": str(uuid.uuid4())})
    print(json.dumps({"configured": True, "version": version, "models": args.model,
                      "model_requests": 0, "global_settings_changed": False}))


if __name__ == "__main__":
    main()
