#!/usr/bin/env python
"""Diagnose Ollama connectivity — especially when it runs on another machine.

    .venv/bin/python scripts/check_ollama.py
    .venv/bin/python scripts/check_ollama.py http://192.168.1.42:11434
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_settings  # noqa: E402
from backend.llm.ollama_provider import OllamaProvider  # noqa: E402

GREEN, RED, AMBER, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _dedupe(names: list[str]) -> list[str]:
    """Roles often share a model; report each tag once."""
    seen: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.append(n)
    return seen


async def probe(url: str, label: str, wanted: list[str]) -> bool:
    wanted = _dedupe(wanted)
    print(f"\n{label}: {url}")
    provider = OllamaProvider(url, timeout=15.0)
    try:
        health = await provider.health()
        if not health.connected:
            print(f"  {RED}✗ unreachable{OFF}")
            print(f"    {health.error}")
            return False

        print(f"  {GREEN}✓ connected{OFF}  v{health.version}  ({health.latency_ms} ms)")
        if health.latency_ms and health.latency_ms > 500:
            print(f"    {AMBER}! high latency — WiFi? a wired link is much better for big models{OFF}")

        print(f"  {DIM}installed: {', '.join(health.models) or '(none)'}{OFF}")

        ok = True
        for model in wanted:
            if model in health.models:
                print(f"  {GREEN}✓{OFF} {model}")
            else:
                print(f"  {RED}✗{OFF} {model} — run on that host: {DIM}ollama pull {model}{OFF}")
                ok = False
        return ok
    finally:
        await provider.aclose()


async def main() -> int:
    settings = get_settings()
    override = sys.argv[1] if len(sys.argv) > 1 else None

    chat_url = override or settings.ollama_base_url
    embed_url = override or settings.embed_base_url

    print(f"{DIM}Gary — Ollama connectivity check{OFF}")

    chat_models = [
        settings.llm_model_large,
        settings.llm_model_fast,
        settings.llm_model_router,
    ]
    split_hosts = embed_url != chat_url
    if not split_hosts:
        # Same box — check the embedding model here too rather than skipping it.
        chat_models.append(settings.embedding_model)

    ok = await probe(chat_url, "chat / reasoning", chat_models)
    if split_hosts:
        ok = await probe(embed_url, "embeddings", [settings.embedding_model]) and ok

    print()
    if ok:
        print(f"{GREEN}All good.{OFF} Start Gary with ./start.sh")
        return 0

    print(f"{AMBER}Issues found.{OFF} Common fixes:")
    print("  • Remote host: start it with  OLLAMA_HOST=0.0.0.0 ollama serve")
    print("  • Local host:  start it with  ollama serve")
    print("  • Check the firewall allows TCP 11434 from this machine")
    print("  • Set the exact tags from `ollama list` in your .env")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
