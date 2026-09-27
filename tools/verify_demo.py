"""Independently reconcile exported research runs and optional deterministic replay.

Usage: python tools/verify_demo.py [outputs/demo] [--replay]
"""
from collections import defaultdict
import hashlib
import json
from math import isclose
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def verify(directory):
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    folders = {directory / name for name in summary["strategies"]}
    runs = [r["run_id"] for r in summary.get("grid", {}).get("trials", [])]
    runs += [r["run_id"] for r in summary.get("ablation", [])]
    for fold in summary.get("walk_forward", []):
        runs.append(fold["test_run_id"])
        runs.extend(r["run_id"] for r in fold["selection_trials"])
    folders.update(directory / "research_runs" / rid for rid in runs)
    checked = []
    for folder in sorted(folders):
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        for name, digest in manifest["files"].items():
            assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest, (folder, name)
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        assert result["metadata"]["data_fingerprint"] == summary["fingerprint"]
        coverage = result["metadata"]["action_verification_check"]
        certificate = result["metadata"]["data_metadata"]["corporate_actions_verification"]
        assert coverage["verified"] is True
        assert set(coverage["symbols"]) <= set(certificate["symbols"])
        assert certificate["start"] <= coverage["start"] <= coverage["end"] <= certificate["end"]
        initial = result["metadata"]["config"]["initial_cash"]
        cash, shares = initial, defaultdict(float)
        events = defaultdict(list)
        for event in result["ledger"]:
            events[event["date"]].append(event)
        for row in result["snapshots"]:
            for event in events[row["date"]]:
                cash += event["cash_delta"]
                if event["symbol"]:
                    shares[event["symbol"]] += event["quantity_delta"]
                assert isclose(cash, event["cash_balance"], abs_tol=1e-6)
            assert isclose(row["cash"], cash, abs_tol=1e-6)
            assert isclose(row["equity"], row["cash"] + row["market_value"] + row["receivables"], abs_tol=1e-6)
            for symbol in shares.keys() | row["positions"].keys():
                assert isclose(shares[symbol], row["positions"].get(symbol, 0), abs_tol=1e-6)
        orders = {o["order_id"]: o for o in result["orders"]}
        for fill in result["fills"]:
            assert fill["signal_date"] < fill["execution_date"]
            order = orders[fill["order_id"]]
            assert order["status"] in {"filled", "partial"}
            assert isclose(order["filled_quantity"], fill["quantity"])
        checked.append(folder)
    return checked


def main():
    args = [arg for arg in sys.argv[1:] if arg != "--replay"]
    directory = Path(args[0]).resolve() if args else ROOT / "outputs" / "demo"
    folders = verify(directory)
    if "--replay" in sys.argv:
        from ashare.demo import run_demo
        before = {(folder.relative_to(directory).as_posix(), file.name): hashlib.sha256(file.read_bytes()).hexdigest()
                  for folder in folders for file in folder.iterdir() if file.is_file()}
        summary_before = (directory / "summary.json").read_bytes()
        run_demo(directory, research=True)
        assert summary_before == (directory / "summary.json").read_bytes(), "Experiment summary changed on replay"
        for (folder, name), digest in before.items():
            assert hashlib.sha256((directory / folder / name).read_bytes()).hexdigest() == digest, (folder, name)
        verify(directory)
    print(json.dumps({"audited_runs": len(folders), "cash_and_positions_reconciled": True,
                      "manifest_hashes_verified": True, "deterministic_replay": "--replay" in sys.argv}, indent=2))


if __name__ == "__main__":
    main()
