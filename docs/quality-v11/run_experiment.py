"""Execute each predeclared v11 phase exactly once (all local; no paid calls).

v11 repeats v10 with one change: the lasting policy's ephemeral floor is 0.55, so
importance 1-2 is dropped as noise again.

Phases:
  luna-lasting   replay the frozen v8 Luna candidates with lasting tiering
  qwen-lasting   replay the frozen v5 qwen candidates with lasting tiering
  eval           make eval's scripted regression, legacy and lasting
  score          unchanged equivalence-v2 scorer, tier checks, frozen rule

Identity routing stays off throughout. Every phase verifies the frozen file hashes
and gate configurations in the manifest and refuses to overwrite earlier evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

MANIFEST = HERE / "experiment-manifest.json"
DATA_MANIFEST = "mnemo/data/quality-v4/manifest.json"
V8 = "docs/quality-v8"
LUNA_PROBE = f"{V8}/extraction.json"
QWEN_PROBE = "docs/quality-v5/extraction-partial-dev.json"
LABELS = ROOT / "docs/quality-v10/durability-labels.json"


def sha(path: Path | str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def env_for(manifest: dict, policy: str) -> dict[str, str]:
    return {**os.environ, **manifest["replay_environment"], **manifest["policies"][policy]}


def preflight(manifest: dict) -> None:
    from mnemo.audit import gate_snapshot
    from mnemo.config import Settings

    changed = [p for p, digest in manifest["frozen_files_sha256"].items() if sha(p) != digest]
    if changed:
        raise SystemExit(f"frozen files changed since the manifest: {changed}")
    for policy, expected in manifest["configuration"].items():
        saved = dict(os.environ)
        os.environ.update(env_for(manifest, policy))
        try:
            if gate_snapshot(Settings()) != expected:
                raise SystemExit(f"gate configuration for {policy} differs from manifest")
        finally:
            os.environ.clear()
            os.environ.update(saved)


def fresh(*paths: Path) -> None:
    existing = [str(p.relative_to(ROOT)) for p in paths if p.exists()]
    if existing:
        raise SystemExit(f"phase already attempted; evidence exists: {existing}")


def run(name: str, command: list[str], env: dict[str, str], output: Path | None) -> int:
    log, journal_path = HERE / f"{name}.log", HERE / f"{name}-run.json"
    fresh(*(p for p in (output, log, journal_path) if p is not None))
    journal = {
        "phase": name,
        "command": command[1:],
        "started_at_utc": now(),
        "manifest_sha256": sha(MANIFEST),
        "complete": False,
    }
    write_json(journal_path, journal)
    started = time.perf_counter()
    with log.open("w") as handle:
        result = subprocess.run(
            command, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT, check=False
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


def replay(name: str, probe: str, policy: str, manifest: dict) -> int:
    output = HERE / f"{name}.json"
    command = [
        sys.executable,
        "scripts/replay_extraction_probe.py",
        "--probe",
        probe,
        "--manifest",
        DATA_MANIFEST,
        "--output",
        str(output.relative_to(ROOT)),
    ]
    return run(name, command, env_for(manifest, policy), output)


def gated_line(path: Path) -> str:
    """make eval's GATED summary without its wall-clock latency."""
    line = next(x for x in path.read_text().splitlines() if x.strip().startswith("GATED"))
    return re.sub(r"\|\s*[\d.]+ ms", "", line).strip()


def event_tiers(replay_report: dict) -> dict[str, str]:
    return {
        w["event_id"]: w["tier"]
        for case in replay_report["cases"].values()
        for w in case["gated"]["writes"]
    }


def target_durability(score: dict, tiers: dict[str, str]) -> dict[str, list[str]]:
    """Historically complete targets, split by whether a credited write is durable."""
    durable: dict[str, bool] = {}
    for entry in score["targets"]:
        evidence = entry["stage_evidence"]
        if not evidence["complete_credit"]["historical"]:
            continue
        events = [d["event_id"] for d in evidence["decisions"] if d and d["event_id"]]
        hit = any(tiers.get(e) == "durable" for e in events)
        durable[entry["logical_target_id"]] = durable.get(entry["logical_target_id"], False) or hit
    return {
        "durable": sorted(t for t, ok in durable.items() if ok),
        "not_durable": sorted(t for t, ok in durable.items() if not ok),
    }


def candidate_tiers(replay_report: dict) -> dict[str, str | None]:
    """Tier of the event each candidate itself wrote (None: rejected or no new event)."""
    tiers = event_tiers(replay_report)
    result: dict[str, str | None] = {}
    for case, report in replay_report["cases"].items():
        own = {w["event_id"] for w in report["gated"]["writes"] if w["op"] in ("ADD", "UPDATE")}
        for d in report["decisions"]:
            ref = f"{case}/{d['turn_id']}/candidate{d['candidate_index']}"
            new = d["event_id"] in own and d["outcome"] in ("accepted", "demoted")
            result[ref] = tiers.get(d["event_id"]) if new else None
    return result


def components(decision: dict) -> dict:
    value = decision.get("score_components")
    while isinstance(value, str):
        value = json.loads(value)
    return value or {}


def score(manifest: dict) -> int:
    from mnemo.eval_equivalence import score as equivalence

    names = ("luna-lasting-score", "qwen-lasting-score", "decision")
    paths = {n: HERE / f"{n}.json" for n in names}
    fresh(*paths.values())
    reviews = {
        "luna": (V8 + "/mustkeep-review.json", V8 + "/source-review.json", LUNA_PROBE),
        "qwen": (
            "docs/quality-v6/mustkeep-review.json",
            "docs/quality-v6/independent-review.json",
            QWEN_PROBE,
        ),
    }
    reports, scores = {}, {}
    for arm in ("luna-lasting", "qwen-lasting"):
        review, support, probe = reviews[arm.split("-")[0]]
        reports[arm] = json.loads((HERE / f"{arm}.json").read_text())
        scores[arm] = equivalence(ROOT / probe, ROOT / review, ROOT / support, HERE / f"{arm}.json")
        write_json(paths[f"{arm}-score"], scores[arm])
    durability = {arm: target_durability(scores[arm], event_tiers(reports[arm])) for arm in scores}
    labels = json.loads(LABELS.read_text())["rows"]
    tiers_by_arm = {"luna-lasting": candidate_tiers(reports["luna-lasting"])}
    label_table = {
        arm: dict(Counter(f"{row['label']}->{tiers.get(row['ref'])}" for row in labels))
        for arm, tiers in tiers_by_arm.items()
    }
    transient_durable = [
        row["ref"]
        for row in labels
        if row["label"] == "transient" and tiers_by_arm["luna-lasting"].get(row["ref"]) == "durable"
    ]
    strict_false = {
        arm: sum(case["gated"]["false_writes"] for case in reports[arm]["cases"].values())
        for arm in reports
    }
    retrieved = {arm: s["unique_complete_by_stage"]["retrieved"] for arm, s in scores.items()}
    low_importance_writes = [
        (arm, d["turn_id"], d["candidate_index"])
        for arm, report in reports.items()
        for case in report["cases"].values()
        for d in case["decisions"]
        if d["outcome"] in ("accepted", "demoted") and components(d).get("importance", 10) < 3
    ]
    rule = manifest["decision_rule"]
    checks = {
        "luna_every_written_target_durable": not durability["luna-lasting"]["not_durable"],
        "qwen_every_written_target_durable": not durability["qwen-lasting"]["not_durable"],
        "luna_lasting_zero_unsupported_writes": scores["luna-lasting"]["historical_write_review"][
            "unsupported"
        ]
        == 0,
        "qwen_lasting_zero_unsupported_writes": scores["qwen-lasting"]["historical_write_review"][
            "unsupported"
        ]
        == 0,
        "strict_false_writes_zero": all(v == 0 for v in strict_false.values()),
        "luna_retrieved_not_below_v8": retrieved["luna-lasting"] >= rule["v8_retrieved"],
        "qwen_retrieved_not_below_control": retrieved["qwen-lasting"]
        >= rule["qwen_control_retrieved"],
        "make_eval_unchanged": gated_line(HERE / "eval-lasting.log")
        == gated_line(HERE / "eval-legacy.log"),
        "noise_band_kept_no_importance_below_3_written": not low_importance_writes,
    }
    decision = {
        "decided_at_utc": now(),
        "rule": rule,
        "checks": checks,
        "outcome": "success" if all(checks.values()) else "failure",
        "target_durability": durability,
        "retrieved": retrieved,
        "write_review": {arm: s["historical_write_review"] for arm, s in scores.items()},
        "strict_false_writes": strict_false,
        "tier_counts": {
            arm: dict(
                Counter(
                    w["tier"]
                    for case in reports[arm]["cases"].values()
                    for w in case["gated"]["writes"]
                )
            )
            for arm in reports
        },
        "reported_not_gated": {
            "luna_label_vs_tier": label_table,
            "transient_labeled_durable_with_lasting": transient_durable,
        },
        "make_eval_gated": {p: gated_line(HERE / f"eval-{p}.log") for p in ("legacy", "lasting")},
    }
    write_json(paths["decision"], decision)
    print(json.dumps({k: decision[k] for k in ("outcome", "checks", "retrieved")}, indent=2))
    return 0


def main() -> None:
    phases = ("luna-lasting", "qwen-lasting", "eval", "score")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phase", choices=phases)
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    preflight(manifest)
    if args.phase == "luna-lasting":
        code = replay(args.phase, LUNA_PROBE, "lasting", manifest)
    elif args.phase == "qwen-lasting":
        code = replay(args.phase, QWEN_PROBE, "lasting", manifest)
    elif args.phase == "eval":
        command = [sys.executable, "-m", "mnemo.eval"]
        code = run("eval-legacy", command, env_for(manifest, "legacy"), None) or run(
            "eval-lasting", command, env_for(manifest, "lasting"), None
        )
    else:
        code = score(manifest)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
