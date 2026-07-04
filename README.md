# LLM-Assisted SAT-Based ATPG

> An end-to-end ATPG pipeline that combines classical SAT solving with LLM agents for intelligent fault collapsing and partial input assignment in digital circuits.

---

## Overview

FMSV automates the test-pattern generation workflow for stuck-at fault models in combinational circuits. Two LLM agents augment the classical SAT-ATPG flow — one performs structural fault collapsing to reduce the fault universe, and the other generates minimal partial PI assignments to guide MiniSAT toward faster solutions. An UNSAT feedback loop retries with adjusted assignments (up to 2 retries) before marking a fault untestable.

```
Circuit JSON  →  Agent 1           →  Agent 2              →  MiniSAT          →  Output CSV
(gold_netlist)   Fault Collapsing     Partial Assignment       (--partial flag)     result_all_agent.csv
                                           ↑
                                    UNSAT → Feedback Loop (max 2 retries)
```

---

## Pipeline Scripts

| Script | Role |
|---|---|
| `gen_gold_mapped.ys` | Yosys script — synthesizes `good.v`, maps to NanGate library, emits `gold_netlist.json` + `mapped.v` |
| `generate_faults.py` | Parses netlist, extracts all valid nets, injects SA0/SA1 faults, writes per-fault JSON to `faults/` |
| `agent_collapse_fault.py` | **Agent 1** — LLM-driven structural fault collapsing (equivalence + symmetry rules) |
| `agent_partial_ass.py` | **Agent 2** — LLM-driven minimal partial PI assignment for fault activation & propagation |
| `make_miter_cnf.py` | Constructs miter circuit and encodes it as DIMACS CNF for MiniSAT |
| `run_sat_all_agent.py` | Orchestrator — runs the full agent + SAT loop, saves `result_collapsed.csv` + `result_all_agent.csv` |
| `run_all_mapped.sh` | Convenience shell script to run the complete pipeline end-to-end |

---

## Tech Stack

| Layer | Tool |
|---|---|
| Logic Synthesis | [Yosys](https://github.com/YosysHQ/yosys) + NanGate 45nm Open Cell Library |
| SAT Solver | [MiniSAT](http://minisat.se/) with `--partial` flag support |
| Fault Collapsing Agent | OpenAI-compatible LLM (configurable model) |
| Partial Assignment Agent | [Groq](https://groq.com/) — `llama-3.3-70b-versatile` |
| Scripting | Python 3.8+ |
| Benchmarks | ISCAS-85 / ISCAS-89 circuits (`c17`, `c432`, `c880`, `c1355`, …) |

---

## Prerequisites

- Python 3.8+
- [Yosys](https://github.com/YosysHQ/yosys) (in PATH)
- MiniSAT binary at `minisat/build/release/bin/minisat`
- OpenAI-compatible API key (for Agent 1)
- Groq API key (for Agent 2)

Install Python dependencies:

```bash
pip install openai
```

---

## Quick Start

### 1. Set environment variables

```bash
export GROQ_API_KEY=your_groq_key
```

### 2. Place your circuit

Drop a synthesizable Verilog file named `good.v` in the project root.

### 3. Run the full pipeline

```bash
./run_all_agent.sh
```

This will:
1. Synthesize and map the circuit with Yosys
2. Enumerate and inject all SA0/SA1 faults
3. Run Agent 1 (fault collapsing) → Agent 2 (partial assignment) → MiniSAT
4. Retry UNSAT faults up to 2 times with adjusted assignments
5. Write results to `result_collapsed.csv` and `result_all_agent.csv`

### 4. Run individual stages

```bash
# Synthesis only
yosys gen_gold_mapped.ys

# Fault generation only
python3 generate_faults.py

# Full agent + SAT loop
python3 run_sat_all_agent.py
```

---

## Output Format

`result_all_agent.csv` columns:

| Column | Description |
|---|---|
| `fault` | Net ID and stuck-at value (e.g. `net_7_SA0`) |
| `agent_status` | `SAT`, `UNSAT_AFTER_RETRIES`, or `ERROR` |
| `restarts` / `conflicts` / `decisions` / `propagations` | MiniSAT metrics |
| `cpu_time_sec` | Solver CPU time |
| `N1[0]` … `Nk[0]` | Partial PI assignments suggested by Agent 2 |

---

## Benchmark Circuits

Standard ISCAS benchmarks are included under `Benchmarks/`:

`c17` · `c432` · `c499` · `c880` · `c1355` · `c3540` · `c6288` · `s298` · `s344`

To run on a benchmark, copy the `.v` file to `good.v` and update the top module name in `gen_gold_mapped.ys`.

---

## Project Structure

```
FMSV_Project/
├── good.v                        # Input circuit (Verilog)
├── gold_netlist.json             # JSON netlist (Yosys output)
├── NangateOpenCellLibrary_typical.lib
├── gen_gold_mapped.ys            # Yosys synthesis script
├── generate_faults.py            # Fault injection
├── agent_collapse_fault.py       # Agent 1 — fault collapsing
├── agent_partial_ass.py          # Agent 2 — partial assignment
├── make_miter_cnf.py             # Miter + CNF encoder
├── run_sat_all_agent.py          # Pipeline orchestrator
├── run_all_agent.sh              # End-to-end shell runner
├── minisat/                      # MiniSAT binary
└── Benchmarks/                   # ISCAS benchmark circuits
```

---

## Known Limitations

- MiniSAT `--partial` flag support must be compiled into the binary; the included build is pre-compiled for Linux x86-64
- LLM fault collapsing quality is model-dependent; larger models yield fewer missed equivalences
- Groq rate limits may throttle Agent 2 on large fault lists
- Feedback loop is capped at 2 retries; deeply sequential or redundant faults may still report `UNSAT_AFTER_RETRIES`

---

