#!/usr/bin/env python3
import argparse
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def find_top_module(design):
    mods = design["modules"]
    for name, mod in mods.items():
        top = mod.get("attributes", {}).get("top")
        if top == 1 or top == "1":
            return name
    return next(iter(mods))


def get_nets_and_names(gold_json_path):
    with open(gold_json_path) as f:
        design = json.load(f)
    mod = design["modules"][find_top_module(design)]

    nets = set()
    bit_to_name = {}
    for name, info in mod.get("netnames", {}).items():
        for b in info.get("bits", []):
            if isinstance(b, int):
                nets.add(b)
                bit_to_name.setdefault(b, name)
    return sorted(nets), bit_to_name


def discover_fault_files(fault_dir):
    paths = sorted(glob.glob(os.path.join(fault_dir, "net_*_SA*.json")))
    out = []
    rx = re.compile(r"net_(\d+)_SA([01])\.json$")
    for p in paths:
        m = rx.search(os.path.basename(p))
        if m:
            out.append((int(m.group(1)), int(m.group(2)), p))
    return out


def parse_minisat_stdout(stdout_text):
    metrics = {
        "restarts": "-",
        "conflicts": "-",
        "decisions": "-",
        "propagations": "-",
        "conflict_literals": "-",
        "memory_mb": "-",
        "cpu_time_sec": "-",
    }
    patterns = {
        "restarts": r"restarts\s*:\s*(\d+)",
        "conflicts": r"conflicts\s*:\s*(\d+)",
        "decisions": r"decisions\s*:\s*(\d+)",
        "propagations": r"propagations\s*:\s*(\d+)",
        "conflict_literals": r"conflict literals\s*:\s*(\d+)",
        "memory_mb": r"Memory used\s*:\s*([0-9.]+)\s*MB",
        "cpu_time_sec": r"CPU time\s*:\s*([0-9.]+)\s*s",
    }
    for key, pat in patterns.items():
        m = re.search(pat, stdout_text, flags=re.IGNORECASE)
        if m:
            metrics[key] = m.group(1)
    return metrics


def parse_result_file(result_path):
    if not os.path.exists(result_path):
        return "ERROR", []
    with open(result_path) as f:
        lines = [line.strip() for line in f if line.strip()]
    if not lines:
        return "ERROR", []
    status = lines[0]
    assignment = []
    if status == "SAT":
        lits = []
        for line in lines[1:]:
            for tok in line.split():
                if tok == "0":
                    break
                try:
                    lits.append(int(tok))
                except ValueError:
                    pass
        assignment = lits
    return status, assignment


def assignment_to_pi_values(assignment, pi_vars):
    lit_map = {abs(x): (1 if x > 0 else 0) for x in assignment}
    out = {}
    for pi_name, var in sorted(pi_vars.items()):
        out[pi_name] = lit_map.get(var, "-")
    return out


def run_cmd(cmd):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc.returncode, proc.stdout


def ensure_clean_dir(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    
def build_fault_list(args, gold_json):
    """Generates a list of all nets to be tested for SA0 and SA1."""
    if args.use_fault_jsons:
        found = discover_fault_files(args.fault_dir)
        return found, True
 
    # This ensures we get EVERY net (internal + ports)
    nets, _ = get_nets_and_names(args.gold_json)
    
    faults = []
    for net in nets:
        for sa in (0, 1):
            faults.append((net, sa, None))
            
    return faults, False


def main():
    ap = argparse.ArgumentParser(description="Run all mapped stuck-at faults and write mapped_result.csv")
    ap.add_argument("--gold-json", default="gold_netlist.json")
    ap.add_argument("--liberty", default="NangateOpenCellLibrary_typical.lib")
    ap.add_argument("--make-miter-script", default="make_miter_cnf.py")
    ap.add_argument("--fault-dir", default="faults")
    ap.add_argument("--work-dir", default="mapped_runs")
    ap.add_argument("--result-file", default="mapped_result.csv")
    ap.add_argument("--use-fault-jsons", action="store_true", help="Use faults/net_*_SA*.json files if already generated")
    args = ap.parse_args()

    minisat = shutil.which("minisat")
    if not minisat:
        print("ERROR: minisat not found in PATH", file=sys.stderr)
        sys.exit(1)

    python_exec = sys.executable or "python3"
    
    # Ensure working directories exist
    ensure_clean_dir(args.work_dir)
    ensure_clean_dir("minisat") # Added to ensure minisat directory exists

    nets, bit_to_name = get_nets_and_names(args.gold_json)
    faults, using_fault_jsons = build_fault_list(args, args.gold_json)

    rows = []

    for idx, (net, sa, faulty_json) in enumerate(faults, start=1):
        base = f"net_{net}_SA{sa}"
        
        # Hardcoded to output sat.cnf inside the minisat folder
        cnf_path = os.path.join("minisat", "sat.cnf") 
        
        # Keep map and logs in mapped_runs to maintain result tracking
        map_path = os.path.join(args.work_dir, base + "_map.json")
        sat_out_path = os.path.join(args.work_dir, base + "_minisat.txt")
        solver_log_path = os.path.join(args.work_dir, base + "_solver.log")

        if using_fault_jsons:
            cmd = [
                python_exec, args.make_miter_script,
                args.gold_json, faulty_json, args.liberty,
                "-o", cnf_path,
                "--map-out", map_path,
            ]
        else:
            cmd = [
                python_exec, args.make_miter_script,
                args.gold_json, args.gold_json, args.liberty,
                "--fault-net", str(net),
                "--sa", str(sa),
                "-o", cnf_path,
                "--map-out", map_path,
            ]

        print(f"[{idx}/{len(faults)}] Building CNF for {base}...")
        rc, out = run_cmd(cmd)
        if rc != 0:
            rows.append({
                "fault": base,
                "net": net,
                "net_name": bit_to_name.get(net, "-"),
                "sa": sa,
                "status": "CNF_ERROR",
                "restarts": "-",
                "conflicts": "-",
                "decisions": "-",
                "propagations": "-",
                "conflict_literals": "-",
                "memory_mb": "-",
                "cpu_time_sec": "-",
            })
            with open(solver_log_path, "w") as f:
                f.write(out)
            continue

        print(f"[{idx}/{len(faults)}] Running MiniSAT for {base}...")
        rc, minisat_stdout = run_cmd([minisat, cnf_path, sat_out_path])
        with open(solver_log_path, "w") as f:
            f.write(minisat_stdout)

        metrics = parse_minisat_stdout(minisat_stdout)
        status, assignment = parse_result_file(sat_out_path)

        pi_vals = {}
        if os.path.exists(map_path):
            with open(map_path) as f:
                mapping = json.load(f)
            pi_vals = assignment_to_pi_values(assignment, mapping.get("pi_vars", {}))

        row = {
            "fault": base,
            "net": net,
            "net_name": bit_to_name.get(net, "-"),
            "sa": sa,
            "status": status,
            "restarts": metrics["restarts"],
            "conflicts": metrics["conflicts"],
            "decisions": metrics["decisions"],
            "propagations": metrics["propagations"],
            "conflict_literals": metrics["conflict_literals"],
            "memory_mb": metrics["memory_mb"],
            "cpu_time_sec": metrics["cpu_time_sec"],
        }
        row.update(pi_vals)
        rows.append(row)

    # stable CSV header
    pi_headers = []
    for n in nets:
        # include only top-level PI names that appear in mapping style like N1[0]
        pass
    discovered_pi_headers = sorted({k for r in rows for k in r.keys() if re.match(r".+\[\d+\]$", k)})
    header = [
        "fault", "net", "net_name", "sa", "status",
        "restarts", "conflicts", "decisions", "propagations",
        "conflict_literals", "memory_mb", "cpu_time_sec",
    ] + discovered_pi_headers

    with open(args.result_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    print(f"Done. Wrote {args.result_file}")


if __name__ == "__main__":
    main()