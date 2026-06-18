"""Execute each predeclared v7 model phase once and record measured wall time."""

import argparse
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from mnemo.config import get_settings
from mnemo.eval_suite import policy_fingerprint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("extraction", "replay"))
    args = parser.parse_args()
    directory = Path(__file__).parent
    output = directory / f"{args.phase}.json"
    log = directory / f"{args.phase}.log"
    journal = directory / f"{args.phase}-run.json"
    if any(p.exists() for p in (output, log, journal)):
        raise ValueError("phase already attempted; preserve evidence and inspect the run budget")
    command = [".venv/bin/python"]
    if args.phase == "extraction":
        command += [
            "scripts/probe_extraction_dev.py",
            "--baseline",
            "docs/quality-v3/external-dev.json",
        ]
    else:
        command += [
            "scripts/replay_extraction_probe.py",
            "--probe",
            str(directory / "extraction.json"),
        ]
    command += ["--manifest", "mnemo/data/quality-v4/manifest.json", "--output", str(output)]
    record = {
        "phase": args.phase,
        "command": command,
        "complete": False,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "policy_sha256": policy_fingerprint(get_settings()),
        "experiment_manifest_sha256": hashlib.sha256(
            (directory / "experiment-manifest.json").read_bytes()
        ).hexdigest(),
        "comparison_baseline": "v5 saved candidates / v6 complete replay, as frozen in manifest",
        "adapter_note": (
            "The extraction driver's embedded before rows are older v3, "
            "not the comparison baseline."
        ),
    }
    journal.write_text(json.dumps(record, indent=2) + "\n")
    started = time.perf_counter()
    with log.open("w") as handle:
        result = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, check=False)
    record.update(
        complete=True,
        exit_code=result.returncode,
        elapsed_seconds=time.perf_counter() - started,
        finished_at_utc=datetime.now(UTC).isoformat(),
    )
    journal.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
