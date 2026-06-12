"""Evaluate source-reviewed conversations in isolated stores with frozen splits."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from mnemo.config import get_settings
from mnemo.embedder import build_embedder
from mnemo.eval import evaluate
from mnemo.eval_data import load_longmemeval
from mnemo.eval_support import EvalDataset
from mnemo.extraction import build_extractor
from mnemo.quality import build_verifier


def load_cases(manifest_path: Path, split: str) -> list[tuple[str, EvalDataset]]:
    manifest = json.loads(manifest_path.read_text())
    entries = [e for e in manifest["records"] if e["split"] == split]
    if not entries:
        raise ValueError(f"no cases for split {split}")
    sessions: set[str] = set()
    for entry in manifest["records"]:
        if sessions.intersection(entry["session_ids"]):
            raise ValueError("split leakage: session reused across records")
        sessions.update(entry["session_ids"])
    cases = []
    for entry in entries:
        source = manifest_path.parent / entry["path"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("source fingerprint changed")
        if "labels" not in entry:
            raise ValueError("source review and frozen labels required before evaluation")
        labels = manifest_path.parent / entry["labels"]
        if hashlib.sha256(labels.read_bytes()).hexdigest() != entry["labels_sha256"]:
            raise ValueError("label fingerprint changed")
        dataset = load_longmemeval(source, labels=json.loads(labels.read_text()), split=split)
        if len(dataset.turns) != entry["turns"]:
            raise ValueError("turn count changed")
        cases.append((entry["id"], dataset))
    return cases


def policy_fingerprint() -> str:
    digest = hashlib.sha256()
    for name in ("extraction.py", "quality.py", "core.py", "config.py"):
        digest.update(name.encode())
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


async def run(args: argparse.Namespace) -> None:
    cases = load_cases(args.manifest, args.split)
    fingerprint = policy_fingerprint()
    if args.split == "holdout":
        if not args.frozen_policy or args.frozen_policy.read_text().strip() != fingerprint:
            raise ValueError("holdout requires a policy hash frozen after dev work")
    settings = get_settings()
    extractor, embedder, verifier = (
        build_extractor(settings),
        build_embedder(settings),
        build_verifier(settings),
    )
    results = {}
    try:
        for identity, dataset in cases:
            print(f"Evaluating {args.split}/{identity}: {len(dataset.turns)} turns", flush=True)
            results[identity] = await evaluate(
                dataset=dataset,
                extractor=extractor,
                embedder=embedder,
                verifier=verifier,
                settings=settings,
            )
            # Preserve completed cases even if a later model call fails.
            args.output.write_text(
                json.dumps(
                    {
                        "split": args.split,
                        "policy_sha256": fingerprint,
                        "complete": False,
                        "cases": results,
                    },
                    indent=2,
                    default=str,
                )
                + "\n"
            )
    finally:
        for component in (extractor, embedder, verifier):
            if hasattr(component, "close"):
                component.close()
    args.output.write_text(
        json.dumps(
            {"split": args.split, "policy_sha256": fingerprint, "complete": True, "cases": results},
            indent=2,
            default=str,
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path(__file__).parent / "data/quality-v2/manifest.json"
    )
    parser.add_argument("--split", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--frozen-policy", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
