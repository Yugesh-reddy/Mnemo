"""Replay a complete saved development probe through the real gate and store."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from mnemo.audit import gate_snapshot
from mnemo.config import get_settings
from mnemo.embedder import build_embedder
from mnemo.eval import _ExtractionFailure, _MaterializedExtractor, evaluate
from mnemo.eval_audit import decoded
from mnemo.eval_normalization import STRICT_SCORING_VERSION
from mnemo.eval_suite import load_cases, policy_fingerprint
from mnemo.eval_support import EvalDataset
from mnemo.extraction import ExtractionBatch
from mnemo.models import ExtractedFact
from mnemo.quality import Verdict, build_verifier


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SavedVerifier:
    """Replay exact source-bound verdicts; absent or conflicting inputs fail closed."""

    model = "saved-development-verdicts"

    def __init__(self, report: dict, cases: list) -> None:
        self.verdicts = {}
        sources = {
            (case, turn.turn_id): turn.text for case, dataset, _ in cases for turn in dataset.turns
        }
        for case, result in report["cases"].items():
            for decision in result["decisions"]:
                verdict = decoded(decision["verification"])
                if verdict is None:
                    continue
                candidate = decoded(decision["candidate"])
                key = (sources[(case, decision["turn_id"])], json.dumps(candidate, sort_keys=True))
                if key in self.verdicts and self.verdicts[key] != verdict:
                    raise ValueError("conflicting cached verdicts")
                self.verdicts[key] = verdict

    def verify(self, candidate: ExtractedFact, source: str) -> Verdict:
        key = (source, json.dumps(candidate.model_dump(mode="json"), sort_keys=True))
        if key not in self.verdicts:
            raise ValueError("no saved verdict for this exact source/candidate")
        return Verdict.model_validate(self.verdicts[key])


def load_probe(probe: Path, manifest: Path) -> list[tuple[str, EvalDataset, list]]:
    saved = json.loads(probe.read_text())
    if saved.get("split") != "dev" or not saved.get("complete"):
        raise ValueError("requires a complete development probe")
    rows = {(r["case"], r["turn_id"]): r for r in saved["rows"]}
    if len(rows) != len(saved["rows"]):
        raise ValueError("duplicate probe source")
    result, expected = [], set()
    for identity, dataset in load_cases(manifest, "dev"):
        turns = tuple(
            t
            for t in dataset.turns
            if t.role == "user" and any(label.disposition == "must_keep" for label in t.labels)
        )
        batches = []
        for turn in turns:
            key = (identity, turn.turn_id)
            expected.add(key)
            if key not in rows:
                raise ValueError("probe omitted a required development source")
            row = rows[key]
            if row["source"] != turn.text or row["labels"] != [
                asdict(label) for label in turn.labels
            ]:
                raise ValueError("probe source or labels differ from frozen development data")
            if row.get("error"):
                batches.append(_ExtractionFailure(row["error"]))
            else:
                batches.append(
                    ExtractionBatch(
                        [ExtractedFact.model_validate(c) for c in row["after"]],
                        row.get("rejections", []),
                    )
                )
        result.append(
            (
                identity,
                replace(
                    dataset,
                    turns=turns,
                    metadata={
                        **dataset.metadata,
                        "scope": "selected must-keep user turns only",
                        "full_dataset_sha256": dataset.fingerprint(),
                    },
                ),
                batches,
            )
        )
    if set(rows) != expected:
        raise ValueError("probe contains sources outside the selected development scope")
    return result


async def run(args: argparse.Namespace) -> None:
    if args.output.exists():
        raise ValueError("preserve prior evidence; use a fresh output path")
    cases = load_probe(args.probe, args.manifest)
    settings = get_settings()
    policy = policy_fingerprint(settings)
    code_paths = [Path(__file__), Path("mnemo/eval.py"), Path("mnemo/eval_normalization.py")]
    code_hashes = {str(p): sha(p) for p in code_paths}
    report: dict[str, Any] = {
        "split": "dev",
        "complete": False,
        "scope": "All saved candidates from 20 selected development turns; not full conversations.",
        "starting_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "policy_sha256": policy,
        "evaluation_code_sha256": code_hashes,
        "probe_sha256": sha(args.probe),
        "manifest_sha256": sha(args.manifest),
        "strict_scoring_version": STRICT_SCORING_VERSION,
        "gate_config": gate_snapshot(settings),
        "query_protocol": (
            "v1: user + frozen predicate + frozen value; k=5; originating session; reinforce=False"
        ),
        "retention_scope": "Immediate reads after selected turns; later-session/TTL unmeasured.",
        "cases": {},
        "verifier_responses": [],
    }

    def save() -> None:
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")

    if args.verdicts_from:
        saved = json.loads(args.verdicts_from.read_text())
        if (
            not saved["complete"]
            or saved["probe_sha256"] != sha(args.probe)
            or saved["policy_sha256"] != policy
        ):
            raise ValueError("cached verdicts must match the complete probe and frozen policy")
        verifier = SavedVerifier(saved, cases)
        report["cached_verdicts"] = {
            "path": str(args.verdicts_from),
            "sha256": sha(args.verdicts_from),
        }
    else:
        verifier = build_verifier(settings)
    embedder = build_embedder(settings)
    fallback = getattr(verifier, "fallback", verifier)
    request = getattr(fallback, "_request", None)
    if request is not None:

        def traced_request(prompt: str) -> str:
            result = request(prompt)
            report["verifier_responses"].append({"prompt": prompt, "response": result})
            save()
            return result

        fallback._request = traced_request
    save()
    try:
        for identity, dataset, batches in cases:
            print(identity, len(dataset.turns), "source turns", flush=True)
            report["cases"][identity] = await evaluate(
                dataset=dataset,
                extractor=_MaterializedExtractor(dataset.turns, batches),
                embedder=embedder,
                verifier=verifier,
                settings=settings,
                probe_retrieval=True,
                probe_all_must_keep=True,
            )
            save()
            print(
                identity, report["cases"][identity]["gated"]["total_events"], "writes", flush=True
            )
    finally:
        for component in (embedder, verifier):
            if hasattr(component, "close"):
                component.close()
    if policy_fingerprint(settings) != policy or any(
        sha(p) != code_hashes[str(p)] for p in code_paths
    ):
        raise RuntimeError("implementation changed during replay; incomplete evidence retained")
    if any(r["metadata"]["cleanup_errors"] for r in report["cases"].values()):
        raise RuntimeError("schema cleanup failed; incomplete evidence retained")
    report["complete"] = True
    save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verdicts-from", type=Path)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
