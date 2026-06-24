"""Execute each predeclared v8 phase exactly once and record what happened.

Phases, in order:
  extraction  one synthetic smoke call, then the unchanged dev probe with Azure Luna
              as the only extractor change (the only paid phase; hard-capped)
  control     replay the frozen v5 qwen candidates under today's code (local)
  replay      replay the Luna candidates (local); requires both blind reviews first
  score       unchanged equivalence-v2 scorer on control and Luna, then the frozen rule

Every phase verifies the frozen file hashes in the manifest and the gate
configuration, and refuses to overwrite earlier evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

MANIFEST = HERE / "experiment-manifest.json"
DATA_MANIFEST = "mnemo/data/quality-v4/manifest.json"
V5_PROBE = "docs/quality-v5/extraction-partial-dev.json"
V6_REVIEW = "docs/quality-v6/mustkeep-review.json"
V6_SUPPORT = "docs/quality-v6/independent-review.json"


def sha(path: Path | str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def preflight(manifest: dict) -> None:
    """Refuse to run unless the frozen implementation and gate policy are in place."""
    os.environ.update(manifest["replay_environment"])
    from mnemo.audit import gate_snapshot
    from mnemo.config import get_settings

    get_settings.cache_clear()
    changed = [p for p, digest in manifest["frozen_files_sha256"].items() if sha(p) != digest]
    if changed:
        raise SystemExit(f"frozen files changed since the manifest: {changed}")
    if gate_snapshot(get_settings()) != manifest["configuration"]:
        raise SystemExit("gate configuration differs from the frozen manifest")


def fresh(*paths: Path) -> None:
    existing = [str(p.relative_to(ROOT)) for p in paths if p.exists()]
    if existing:
        raise SystemExit(f"phase already attempted; evidence exists: {existing}")


def extraction(manifest: dict) -> int:
    from mnemo.config import get_settings
    from scripts import probe_extraction_dev as probe
    from scripts.azure_extractor import AzureExtractor

    output, log = HERE / "extraction.json", HERE / "extraction.log"
    journal_path, exchanges_path = HERE / "extraction-run.json", HERE / "extraction-exchanges.json"
    fresh(output, log, journal_path, exchanges_path)
    settings = get_settings()
    if not (settings.azure_endpoint and settings.azure_deployment and settings.azure_api_key):
        raise SystemExit("MNEMO_AZURE_ENDPOINT, MNEMO_AZURE_DEPLOYMENT and API key are required")
    extractor = AzureExtractor(
        endpoint=settings.azure_endpoint,
        deployment=settings.azure_deployment,
        api_key=settings.azure_api_key.get_secret_value(),
        max_retries=settings.extractor_max_retries,
        timeout=settings.extractor_timeout_seconds,
    )
    journal: dict = {
        "phase": "extraction",
        "started_at_utc": now(),
        "manifest_sha256": sha(MANIFEST),
        "extractor": {
            "provider": "azure",
            "deployment": settings.azure_deployment,
            "base_url": extractor.base_url,
            "reasoning_effort": "none",
            "max_completion_tokens": 2048,
        },
        "command": [
            "scripts/probe_extraction_dev.py",
            "--baseline",
            "docs/quality-v3/external-dev.json",
            "--manifest",
            DATA_MANIFEST,
            "--output",
            str(output.relative_to(ROOT)),
        ],
        "complete": False,
    }
    write_json(journal_path, journal)
    started = time.perf_counter()
    exit_code = 1
    try:
        journal["smoke"] = extractor.smoke()
        journal["temperature_rule"] = (
            "temperature=0 sent" if extractor.send_temperature else "temperature omitted (400)"
        )
        write_json(journal_path, journal)
        probe.build_extractor = lambda _settings=None: extractor
        sys.argv = ["probe_extraction_dev.py", *journal["command"][1:]]
        with log.open("w") as handle, contextlib.redirect_stdout(handle):
            probe.main()
        exit_code = 0
    except BaseException as exc:  # Record every failure; never retry a paid phase.
        journal["error"] = "".join(traceback.format_exception(exc))
    finally:
        journal.update(
            complete=True,
            exit_code=exit_code,
            elapsed_seconds=time.perf_counter() - started,
            finished_at_utc=now(),
            usage=extractor.usage,
            confirmed_models=sorted(
                {
                    x["response"].get("model")
                    for x in extractor.exchanges
                    if "model" in x.get("response", {})
                }
            ),
            infrastructure_retries=sum(1 for x in extractor.exchanges if x["attempt"] > 0),
            budget_exhausted=extractor.fatal_error or None,
        )
        write_json(exchanges_path, extractor.exchanges)
        write_json(journal_path, journal)
    print(
        json.dumps({k: journal[k] for k in ("exit_code", "usage", "temperature_rule")}, default=str)
    )
    return exit_code


def replay(name: str, probe_path: str, manifest: dict, *, requires: tuple[Path, ...] = ()) -> int:
    output, log, journal_path = (
        HERE / f"{name}.json",
        HERE / f"{name}.log",
        HERE / f"{name}-run.json",
    )
    fresh(output, log, journal_path)
    missing = [str(p.relative_to(ROOT)) for p in requires if not p.exists()]
    if missing:
        raise SystemExit(f"blind reviews must exist before replay: {missing}")
    command = [
        sys.executable,
        "scripts/replay_extraction_probe.py",
        "--probe",
        probe_path,
        "--manifest",
        DATA_MANIFEST,
        "--output",
        str(output.relative_to(ROOT)),
    ]
    journal = {
        "phase": name,
        "command": command[1:],
        "started_at_utc": now(),
        "manifest_sha256": sha(MANIFEST),
        "probe_sha256": sha(probe_path),
        "reviews_sha256_before_replay": {str(p.relative_to(ROOT)): sha(p) for p in requires},
        "complete": False,
    }
    write_json(journal_path, journal)
    started = time.perf_counter()
    with log.open("w") as handle:
        result = subprocess.run(
            command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, check=False
        )
    journal.update(
        complete=True,
        exit_code=result.returncode,
        elapsed_seconds=time.perf_counter() - started,
        finished_at_utc=now(),
    )
    write_json(journal_path, journal)
    print(json.dumps(journal, indent=2))
    return result.returncode


def score(manifest: dict) -> int:
    from mnemo.eval_equivalence import score as equivalence

    paths = [HERE / n for n in ("control-score.json", "score.json", "decision.json")]
    fresh(*paths)
    control = equivalence(
        ROOT / V5_PROBE, ROOT / V6_REVIEW, ROOT / V6_SUPPORT, HERE / "control-replay.json"
    )
    luna = equivalence(
        HERE / "extraction.json",
        HERE / "mustkeep-review.json",
        HERE / "source-review.json",
        HERE / "replay.json",
    )
    write_json(paths[0], control)
    write_json(paths[1], luna)
    rule = manifest["decision_rule"]
    retrieved = luna["unique_complete_by_stage"]["retrieved"]
    checks = {
        "retrieved_exceeds_frozen_baseline": retrieved > rule["frozen_baseline_retrieved"],
        "retrieved_exceeds_same_code_control": retrieved
        > control["unique_complete_by_stage"]["retrieved"],
        "zero_unsupported_historical_writes": luna["historical_write_review"]["unsupported"] == 0,
    }
    summary = {
        name: {
            "unique_complete_by_stage": s["unique_complete_by_stage"],
            "denominator": s["unique_target_denominator"],
            "candidate_coverage": s["candidate_coverage"],
            "historical_write_review": {
                k: v for k, v in s["historical_write_review"].items() if k != "supported_precision"
            },
            "loss_counts_latest_occurrence": s["loss_counts_latest_occurrence"],
        }
        for name, s in (("control", control), ("luna", luna))
    }
    decision = {
        "decided_at_utc": now(),
        "rule": rule,
        "checks": checks,
        "outcome": "success" if all(checks.values()) else "failure",
        "summary": summary,
    }
    write_json(paths[2], decision)
    print(
        json.dumps({"outcome": decision["outcome"], "checks": checks, "summary": summary}, indent=2)
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phase", choices=("extraction", "control", "replay", "score"))
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    preflight(manifest)
    if args.phase == "extraction":
        code = extraction(manifest)
    elif args.phase == "control":
        code = replay("control-replay", V5_PROBE, manifest)
    elif args.phase == "replay":
        code = replay(
            "replay",
            "docs/quality-v8/extraction.json",
            manifest,
            requires=(HERE / "source-review.json", HERE / "mustkeep-review.json"),
        )
    else:
        code = score(manifest)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
