import json
import sys
import os
import re
from openai import OpenAI

# ── Configuration ───────────────────────────────────────────────────────────
MODEL_ID = "llama-3.3-70b-versatile" 
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

# ── System Prompt ───────────────────────────────────────────────────────────
# Updated to rely on the dynamic Gate Logic Reference rather than hardcoded basic gates.

SYSTEM_PROMPT = """You are a master VLSI test engineer and ATPG heuristic specialist.
Your task is to find the strictly MINIMAL partial Primary Input (PI) assignments required to both ACTIVATE a stuck-at fault and PROPAGATE it to a Primary Output (PO).

ATPG requires two phases:

PHASE 1: ACTIVATION RULES
To trigger the fault, you must drive the target net to the opposite of its stuck-at value.
1. Target is Stuck-At-0 (SA0) -> You must drive target net to 1.
2. Target is Stuck-At-1 (SA1) -> You must drive target net to 0.

PHASE 2: PROPAGATION (SENSITIZATION) RULES
To observe the fault, the signal must flow from the target net forward to at least one Primary Output (PO). 
For the signal to pass through a gate on this path, all OTHER inputs to that gate (side-inputs) MUST be set to their Non-Controlling Values (NCV).

BACKWARD JUSTIFICATION & COMPLEX GATES:
- You will be provided with a "GATE LOGIC REFERENCE" in the prompt. This defines the boolean equation for every cell type in the circuit.
- To justify a required output value (0 or 1) for ANY gate, look up its equation in the reference.
- Determine the strictly MINIMAL input assignments required to satisfy that equation (e.g., for an AOI21 gate `NOT((B1 & B2) | A)`, to get a 1 at the output, you need `(B1 & B2) | A` to be 0, which means `A=0` AND either `B1=0` OR `B2=0`).
- For propagation, ensure the side-inputs allow the fault signal to toggle the output of the gate. Use the cell's equation to deduce the required NCVs.

INSTRUCTIONS:
1. ACTIVATION: Start at the target net, determine the activation value, and trace backward to PIs using the gate equations.
2. PROPAGATION: Find ONE forward path from the target net to a PO. 
3. Identify the side-inputs for the gates on that forward path, determine their required Non-Controlling Values using the equations, and trace them backward to PIs.
4. Combine the PIs from step 1 and step 3. DO NOT assign values to any other PIs.

You must wrap your step-by-step logic trace inside <thinking>...</thinking> XML tags.
After the thinking block, output ONLY a valid JSON dictionary of the required PIs.

Example format:
<thinking>
1. Activation: Target net_5 SA0. Need net_5 = 1. net_5 is PI N1. So N1 = 1.
2. Propagation: Path is net_5 -> AND2 -> PO N9. 
3. The AND2 gate has a side-input net_6. Equation: output = A1 & A2. NCV is 1. Need net_6 = 1. net_6 is PI N2. So N2 = 1.
4. Final minimal PIs: N1=1, N2=1.
</thinking>
{
  "N1": 1,
  "N2": 1
}"""

# ── Bulletproof JSON Extractor ──────────────────────────────────────────────

def extract_json_from_text(text: str) -> dict:
    text_without_thoughts = re.sub(r'<thinking>[\s\S]*?</thinking>', '', text)
    match = re.search(r'\{[\s\S]*\}', text_without_thoughts)
    if not match:
        match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        raise ValueError(f"No JSON object found in LLM response:\n{text}")
    
    clean_json_str = match.group(0)
    return json.loads(clean_json_str)

# ── Groq Helper ─────────────────────────────────────────────────────────────

def get_partial_assignment(prompt: str) -> dict:
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        response = client.chat.completions.create(
            model=MODEL_ID,
            messages=messages,
            temperature=0.0 
        )
        reply = response.choices[0].message.content
        return extract_json_from_text(reply)
    except Exception as e:
        print(f"\n[API ERROR] {e}", file=sys.stderr)
        sys.exit(1)

# ── Netlist Parsing ─────────────────────────────────────────────────────────

def load_netlist(json_path: str) -> dict:
    with open(json_path) as f:
        return json.load(f)

def extract_tight_netlist(netlist: dict) -> dict:
    modules = netlist.get("modules", {})
    if not modules:
        raise ValueError("Invalid Yosys JSON")
    
    top_module = next(iter(modules.values()))
    ports = top_module.get("ports", {})
    cells = top_module.get("cells", {})

    bit_to_name = {}
    primary_inputs = []

    for port_name, port_data in ports.items():
        if port_data["direction"] == "input":
            primary_inputs.append(port_name)
        for bit in port_data["bits"]:
            bit_to_name[bit] = port_name

    gates = []
    for cell_name, cell_data in cells.items():
        gate_type = cell_data["type"]
        connections = cell_data["connections"]
        
        gate_in = []
        gate_out = []
        
        for pin_name, bits in connections.items():
            is_out = pin_name in ["Y", "ZN", "Z", "Q", "S", "CO"] # Added S and CO for adders
            for bit in bits:
                if isinstance(bit, int) and bit not in bit_to_name:
                    bit_to_name[bit] = f"net_{bit}"
                
                mapped_name = bit_to_name.get(bit, str(bit)) 
                if is_out:
                    gate_out.append(mapped_name)
                else:
                    gate_in.append(mapped_name)
        
        gates.append({
            "type": gate_type,
            "in": gate_in,
            "out": gate_out[0] if len(gate_out) == 1 else gate_out 
        })

    return {
        "PIs": primary_inputs,
        "gates": gates
    }

def write_minisat_partial_assignment(llm_assignment: dict, dynamic_map_path: str):
    if not os.path.exists(dynamic_map_path):
        print(f"Error: Could not find mapping file {dynamic_map_path}", file=sys.stderr)
        return

    with open(dynamic_map_path, "r") as f:
        miter_map = json.load(f)

    pi_vars = miter_map.get("pi_vars", {})
    
    name_to_var_id = {}
    for mapped_name, var_id in pi_vars.items():
        clean_name = mapped_name.split('[')[0] 
        name_to_var_id[clean_name] = var_id

    os.makedirs("minisat", exist_ok=True)
    output_path = os.path.join("minisat", "partial.txt")

    literals = []
    for port_name, value in llm_assignment.items():
        if port_name not in name_to_var_id:
            continue
        
        var_id = name_to_var_id[port_name]
        dimacs_literal = f"{var_id}" if value == 1 else f"-{var_id}"
        literals.append(dimacs_literal)
            
    with open(output_path, "w") as f:
        if literals:
            f.write(" ".join(literals) + " 0\n")
        else:
            f.write("0\n")  

# ── Main Execution ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    NETLIST_PATH = "gold_netlist.json" 
    GATE_LOGIC_PATH = "gate_logic.txt"
    
    if not os.path.exists(NETLIST_PATH):
        print(f"Error: {NETLIST_PATH} not found.", file=sys.stderr)
        sys.exit(1)

    # 1. Parse Command Line Args
    if len(sys.argv) >= 3:
        try:
            TARGET_BIT = int(sys.argv[1])
            FAULT_VAL = int(sys.argv[2])
        except ValueError:
            print("Error: Target bit and fault value must be integers.", file=sys.stderr)
            sys.exit(1)
    else:
        TARGET_BIT = 5
        FAULT_VAL = 0

    # 2. Construct dynamic path and load the fault-specific mapping
    MAP_FILENAME = f"net_{TARGET_BIT}_SA{FAULT_VAL}_map.json"
    MAP_FILEPATH = os.path.join("mapped_runs", MAP_FILENAME)

    if not os.path.exists(MAP_FILEPATH):
        print(f"Error: Required map file {MAP_FILEPATH} not found.", file=sys.stderr)
        sys.exit(1)

    with open(MAP_FILEPATH, "r") as f:
        fault_map_data = json.load(f)

    # 3. Determine TARGET_NET name
    # We look for the net associated with the target bit in the pi_vars or 
    # use the naming convention directly since it's now standardized.
    TARGET_NET = f"net_{TARGET_BIT}"

    # Load gate logic reference
    gate_logic_content = ""
    if os.path.exists(GATE_LOGIC_PATH):
        with open(GATE_LOGIC_PATH, "r") as f:
            gate_logic_content = f.read().strip()

    # 4. Process Netlist
    netlist_data = load_netlist(NETLIST_PATH)
    circuit_topology = extract_tight_netlist(netlist_data)

    # 5. Build Prompt
    prompt = (
        f"Target Fault: {TARGET_NET} Stuck-At-{FAULT_VAL}\n\n"
        f"GATE LOGIC REFERENCE:\n{gate_logic_content}\n\n"
        f"Circuit Topology:\n{json.dumps(circuit_topology, indent=2)}\n\n"
        "1. Trace the backward path to activate the fault.\n"
        "2. Find a forward propagation path and determine the side-input NCVs.\n"
        "3. Output the JSON dictionary of the strictly MINIMAL required PIs."
    )

    # 6. Get LLM Assignment
    final_assignment = get_partial_assignment(prompt)
    
    print("--- LLM Recommended PI Assignment ---")
    print(json.dumps(final_assignment, indent=2))

    # 7. Write to MiniSAT format using the dynamic map file
    write_minisat_partial_assignment(final_assignment, MAP_FILEPATH)
    print(f"\nSuccess: Partial assignment written to minisat/partial.txt using {MAP_FILENAME}")