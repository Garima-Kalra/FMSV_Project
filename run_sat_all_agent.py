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


# ==========================================
# NEW FUNCTION: Run Agent collapse fault Internally
# ==========================================
def run_agent_collapse_fault_internally(agent_collapse_fault_script, gold_json, prompt_file):
    print(f"--- Running {agent_collapse_fault_script} internally to collapse faults ---")
    cmd = ["python3", agent_collapse_fault_script, gold_json, "--prompt-file", prompt_file]
    
    # We capture stdout to get the JSON, but let stderr print to the terminal so we see logs/errors
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    
    if proc.returncode != 0:
        print("ERROR: Agent collapse fault failed to execute properly.")
        sys.exit(1)
        
    try:
        # Agent collapse fault outputs pure JSON to stdout
        output_json = json.loads(proc.stdout.strip())
        collapsed_list = output_json.get("collapsed_fault_list", [])
        
        # Check if the python enforcer caught anything and print it
        enforcer_msg = output_json.get("_python_enforcer_")
        if enforcer_msg:
            print(f"Agent collapse fault Enforcer: {enforcer_msg}")
            
        return [(f["net"], f["sa"], None) for f in collapsed_list]
    except json.JSONDecodeError as e:
        print("ERROR: Failed to parse Agent collapse fault output as JSON.")
        print("Raw Output:\n", proc.stdout)
        sys.exit(1)


def run_agent_and_partial(agent_script, net, sa, map_path):
    """
    Closed-Loop Evaluation:
    1. Run agent -> produce partial assignment
    2. Run minisat with -partial
    3. If UNSAT, feed the failure back to the agent and retry (Max 3 times).
    """
    MAX_RETRIES = 2
    minisat_bin = os.path.join(os.getcwd(), "minisat", "build", "release", "bin", "minisat")
    
    # We will store the history of failed attempts to pass to the LLM
    failure_history = []
    
    # Create empty metrics if it fails completely
    empty_metrics = {
        "restarts": "-", "conflicts": "-", "decisions": "-",
        "propagations": "-", "conflict_literals": "-",
        "memory_mb": "-", "cpu_time_sec": "-"
    }
    
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"    -> [Loop {attempt}/{MAX_RETRIES}] Consulting LLM for partial assignment...")
        
        # -------- Run Agent (Pass history as a JSON string argument) --------
        history_str = json.dumps(failure_history)
        cmd = ["python3", agent_script, str(net), str(sa), history_str]

        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if proc.returncode != 0:
            print(f"    -> Agent Error on attempt {attempt}")
            return "AGENT_ERROR", [], empty_metrics

        # Assuming your agent still writes to minisat/partial.txt
        partial_file = "minisat/partial.txt"
        
        # Read what the LLM actually guessed so we can log it if it fails
        if os.path.exists(partial_file):
            with open(partial_file, "r") as pf:
                llm_guess = pf.read().strip()
        else:
            llm_guess = "No assignment generated"

        # -------- Run MiniSAT with partial --------
        result_file = "minisat/agent_result.txt"
        
        # Added text=True to correctly capture the output string
        sat_proc = subprocess.run([
            minisat_bin,
            "minisat/sat.cnf",
            result_file,
            partial_file,
            "-partial"
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Parse the metrics from the SAT solver's output
        metrics = parse_minisat_stdout(sat_proc.stdout)
        status, final_assignment = parse_result_file(result_file)

        # -------- Evaluate the Loop --------
        if status == "SAT":
            print(f"    -> SUCCESS! LLM guided solver to SAT on attempt {attempt}.")
            return status, final_assignment, metrics
        elif status == "UNSAT":
            print(f"    -> FAILURE: LLM's assignment caused a conflict. Extracting feedback...")
            # Record this failure so the LLM knows not to try this path again
            failure_history.append({
                "attempt": attempt,
                "failed_assignment": llm_guess,
                "feedback": "This combination of partial inputs resulted in an UNSAT conflict. You must find an alternative sensitization path."
            })
        else:
            print(f"    -> TIMEOUT/ERROR: Solver struggled.")
            return status, [], metrics

    print("    -> LLM Failed to find a valid path after maximum retries.")
    return "UNSAT_AFTER_RETRIES", [], empty_metrics

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold-json", default="gold_netlist.json")
    ap.add_argument("--liberty", default="NangateOpenCellLibrary_typical.lib")
    ap.add_argument("--make-miter-script", default="make_miter_cnf.py")
    ap.add_argument("--fault-dir", default="faults")
    ap.add_argument("--work-dir", default="mapped_runs")
    ap.add_argument("--result-file", default="result_collapsed.csv")
    ap.add_argument("--use-fault-jsons", action="store_true")
    
    # Optional manual file loading
    ap.add_argument("--agent_collapse_fault-json", help="Path to JSON output from Agent collapse fault (contains collapsed faults)")
    
    # Automated internal execution parameters
    ap.add_argument("--run-agent_collapse_fault", action="store_true", help="Automatically run Agent collapse fault to generate the collapsed fault list")
    ap.add_argument("--agent_collapse_fault-script", default="agent_collapse_fault.py", help="Path to the Agent collapse fault script (default: agent_collapse_fault.py)")
    ap.add_argument("--agent_collapse_fault-prompt", default="gate_logic.txt", help="Path to the Agent collapse fault prompt file (default: gate_logic.txt)")
    
    args = ap.parse_args()

    minisat = os.path.join(os.getcwd(), "minisat", "build", "release", "bin", "minisat")
    if not minisat:
        print("ERROR: minisat not found")
        sys.exit(1)

    ensure_clean_dir(args.work_dir)
    ensure_clean_dir("minisat")

    nets, bit_to_name = get_nets_and_names(args.gold_json)

    # -------- RESOLVE FAULT LIST --------
    if args.run_agent_collapse_fault:
        # Run Agent collapse fault live
        faults = run_agent_collapse_fault_internally(args.agent_collapse_fault_script, args.gold_json, args.agent_collapse_fault_prompt)
        print(f"-> Agent collapse fault finished successfully. Proceeding with {len(faults)} collapsed faults.\n")
    elif args.agent_collapse_fault_json:
        # Load from previously saved file
        print(f"Loading collapsed faults from {args.agent_collapse_fault_json}...")
        with open(args.agent_collapse_fault_json, "r") as f:
            a4_data = json.load(f)
        collapsed_list = a4_data.get("collapsed_fault_list", [])
        faults = [(f["net"], f["sa"], None) for f in collapsed_list]
        print(f"-> Agent collapse fault collapsed the list to {len(faults)} faults.")
    elif args.use_fault_jsons:
        faults = discover_fault_files(args.fault_dir)
    else:
        # Exhaustive default
        faults = [(n, sa, None) for n in nets for sa in (0, 1)]

    rows = []
    agent_rows = []

    for idx, (net, sa, faulty_json) in enumerate(faults, 1):
        base = f"net_{net}_SA{sa}"
        print(f"[{idx}/{len(faults)}] {base}")

        cnf_path = "minisat/sat.cnf"
        map_path = os.path.join(args.work_dir, base + "_map.json")
        sat_out = os.path.join(args.work_dir, base + "_minisat.txt")

        # CNF
        cmd = [
            sys.executable, args.make_miter_script,
            args.gold_json, args.gold_json, args.liberty,
            "--fault-net", str(net), "--sa", str(sa),
            "-o", cnf_path, "--map-out", map_path
        ]
        rc, _ = run_cmd(cmd)
        if rc != 0:
            continue

        # MiniSAT
        _, out = run_cmd([minisat, cnf_path, sat_out])
        metrics = parse_minisat_stdout(out)
        status, assignment = parse_result_file(sat_out)

        pi_vals = {}
        if os.path.exists(map_path):
            mapping = json.load(open(map_path))
            pi_vals = assignment_to_pi_values(assignment, mapping.get("pi_vars", {}))

        # -------- ORIGINAL CSV --------
        row = {
            "fault": base, "net": net, "net_name": bit_to_name.get(net, "-"),
            "sa": sa, "status": status
        }
        row.update(metrics)
        row.update(pi_vals)
        rows.append(row)

        # -------- AGENT FLOW --------
        # Unpack the metrics
        agent_status, agent_assignment, agent_metrics = run_agent_and_partial("agent_partial_ass.py", net, sa, map_path)

        agent_row = {
            "fault": base,
            "agent_status": agent_status
        }
        
        # Add the SAT solver metrics to the agent row
        agent_row.update(agent_metrics)

        # convert assignment to readable
        agent_pi_vals = assignment_to_pi_values(agent_assignment, mapping.get("pi_vars", {}))
        agent_row.update(agent_pi_vals)

        agent_rows.append(agent_row)

    # -------- SAVE ORIGINAL --------
    if rows:
        with open(args.result_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    # -------- SAVE AGENT CSV --------
    if agent_rows:
        with open("result_all_agent.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=agent_rows[0].keys())
            writer.writeheader()
            writer.writerows(agent_rows)

    print("\n Done")
    print("result_collapsed.csv + result_all_agent.csv generated")


if __name__ == "__main__":
    main()