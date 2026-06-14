"""Evaluate source-reviewed conversations in isolated stores with frozen splits."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from mnemo.audit import gate_snapshot
from mnemo.config import Settings, get_settings
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


def policy_fingerprint(settings: Settings | None = None) -> str:
    digest = hashlib.sha256()
    for name in ("extraction.py", "quality.py", "core.py", "config.py", "models.py"):
        digest.update(name.encode())
        digest.update(Path(__file__).with_name(name).read_bytes())
    digest.update(json.dumps(gate_snapshot(settings or get_settings()), sort_keys=True).encode())
    return digest.hexdigest()


async def run(args: argparse.Namespace) -> None:
    cases = load_cases(args.manifest, args.split)
    settings = get_settings()
    fingerprint = policy_fingerprint(settings)
    if args.split == "holdout":
        if not args.frozen_policy or args.frozen_policy.read_text().strip() != fingerprint:
            raise ValueError("holdout requires a policy hash frozen after dev work")
    results = {}
    if args.output.exists():
        if not args.resume:
            raise ValueError("output already exists; use --resume for the same frozen policy")
        saved = json.loads(args.output.read_text())
        if saved["policy_sha256"] != fingerprint or saved["split"] != args.split:
            raise ValueError("cannot resume results from a different policy or split")
        results = saved["cases"]
        if any(r["metadata"]["cleanup_errors"] for r in results.values()):
            raise ValueError("cannot resume a report with unresolved cleanup errors")
        for identity, dataset in cases:
            if identity in results and (
                results[identity]["metadata"]["fingerprints"]["dataset"] != dataset.fingerprint()
            ):
                raise ValueError("cannot resume results with changed data or labels")
    extractor, embedder, verifier = (
        build_extractor(settings),
        build_embedder(settings),
        build_verifier(settings),
    )
    try:
        for identity, dataset in cases:
            if identity in results:
                continue
            print(f"Evaluating {args.split}/{identity}: {len(dataset.turns)} turns", flush=True)
            results[identity] = await evaluate(
                dataset=dataset,
                extractor=extractor,
                embedder=embedder,
                verifier=verifier,
                settings=settings,
                probe_retrieval=True,
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
            if results[identity]["metadata"]["cleanup_errors"]:
                raise RuntimeError("evaluation cleanup failed; partial report retained")
    finally:
        for component in (extractor, embedder, verifier):
            if hasattr(component, "close"):
                component.close()
    if policy_fingerprint(settings) != fingerprint:
        raise RuntimeError("policy changed during evaluation; partial report retained")
    has_errors = any(r["metadata"].get("status") == "scored_with_errors" for r in results.values())
    args.output.write_text(
        json.dumps(
            {
                "split": args.split,
                "policy_sha256": fingerprint,
                "complete": True,
                "status": "scored_with_errors" if has_errors else "scored",
                "cases": results,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    if has_errors:
        raise RuntimeError("evaluation scored with failed turns; complete report retained")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path(__file__).parent / "data/quality-v2/manifest.json"
    )
    parser.add_argument("--split", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--frozen-policy", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
