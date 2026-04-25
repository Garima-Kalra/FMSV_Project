#!/usr/bin/env python3
import json
import sys
import os
import re
import argparse
import logging
from typing import Dict, Any
from openai import OpenAI

# ==============================
# Enhanced Default Prompt (VLSI)
# ==============================
DEFAULT_SYSTEM_PROMPT = """
You are an expert VLSI Test Engineer specializing in Design for Testability (DFT) and structural fault modeling.

Your task is to perform optimal structural fault collapsing (Equivalence and optionally Dominance) on a given logic circuit graph.

INPUT FORMAT:
You will receive a JSON object representing a digital circuit. It contains the standard logic gates, inputs, and outputs. Note that gate types may appear as raw standard cell names from a PDK (e.g., sky130_...) or generic Yosys internal cells. Infer their logical function based on their names.

FAULT COLLAPSING RULES:
1. Base Case: Assume every net (wire) has both Stuck-At-0 (SA0) and Stuck-At-1 (SA1) faults.
2. Equivalence Collapsing:
   - AND/NAND: All input SA0 faults are equivalent to the output SA0 (AND) or SA1 (NAND). Keep only one representative.
   - OR/NOR: All input SA1 faults are equivalent to the output SA1 (OR) or SA0 (NOR). Keep only one representative.
   - NOT/INV: Input SA0 == Output SA1; Input SA1 == Output SA0. Keep only one representative.
3. Symmetry Collapsing: 
   - For structurally symmetric gates (e.g., AND, OR, NAND, NOR), the inputs are interchangeable. Faults of the same value on interchangeable inputs are symmetrically equivalent. Keep only one representative.
4. Fanout Branches: Faults on fanout branches cannot automatically be collapsed with the stem. Treat them distinctly.

CRITICAL OUTPUT REQUIREMENTS (MANDATORY):
You MUST respond in JSON format only.
- Output MUST be a valid JSON object.
- Do NOT include any explanation outside JSON.
- If you output anything other than JSON, the result is invalid.

Return ONLY a valid JSON object in this exact structure:

{
  "reasoning": "You MUST analyze the circuit EXHAUSTIVELY, gate by gate. For EVERY single gate in the circuit, you must write a new line. Format: 'Gate [Name] ([Type]): Inputs [X], Output [Y]. Applying equivalence: [Rule]. Dropping X@0... Applying symmetry: [Rule]. Dropping Y@1...'. You MUST explicitly state 'Dropping net@sa' for EVERY single fault you reduce.",
  "collapsed_fault_list": []
}
"""

# ==============================
# Core LLM Engine
# ==============================
class GenericJSONAgent:
    def __init__(self, model_id: str, api_key: str):
        self.model_id = model_id
        self.client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=api_key
        )

    def _extract_json(self, text: str) -> Dict[str, Any]:
        """Safely extracts JSON objects from LLM outputs, bypassing markdown wrappers."""
        text = text.strip()
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'```$', '', text).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Fallback regex for robust extraction of the outermost JSON object
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError as e:
                raise ValueError(f"Found object pattern, but invalid JSON: {e}")

        raise ValueError(f"No valid JSON found in LLM output.\nRaw Output:\n{text}")

    def process(self, input_data: Any, system_prompt: str) -> Dict[str, Any]:
        """Sends arbitrary data to the LLM with a specific system prompt."""
        logging.info(f"Generating completion using model: {self.model_id}")
        
        # Safety check: ensure "json" is in the prompt for the API to accept it
        if "json" not in system_prompt.lower():
            system_prompt += "\n\nYou MUST return the response in JSON format."

        response = self.client.chat.completions.create(
            model=self.model_id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(input_data, indent=2)}
            ],
            temperature=0,
            max_tokens= 8000,
            response_format={"type": "json_object"}
        )

        output_text = response.choices[0].message.content
        logging.debug(f"Raw LLM Output:\n{output_text}")
        
        return self._extract_json(output_text)

# ==============================
# Helper Pre-processors (Domain Specific)
# ==============================
def compress_yosys_json(circuit_json: Dict[str, Any]) -> Dict[str, Any]:
    """Compresses a Yosys JSON output, passing gate types exactly as-is."""
    if not isinstance(circuit_json, dict) or "modules" not in circuit_json:
        return circuit_json # Assume it's already generic/compressed

    module = list(circuit_json["modules"].values())[0]
    gates, inputs_list, outputs_list = [], [], []

    for cell in module.get("cells", {}).values():
        ctype = cell.get("type", "")
        conn = cell.get("connections", {})
        
        inputs = []
        outputs = []
        # Common output pin names in standard cell libraries
        output_pin_names = {"ZN", "Y", "X", "Q", "OUT"} 
        
        for k, val in conn.items():
            if k in output_pin_names:
                outputs.extend(val)
            elif k not in {"VGND", "VPWR", "VSS", "VDD"}: # Ignore power pins
                inputs.extend(val)

        output = outputs[0] if outputs else None

        gates.append({"type": ctype, "in": inputs, "out": output})

    for port in module.get("ports", {}).values():
        target = inputs_list if port.get("direction") == "input" else outputs_list
        target.extend(port.get("bits", []))

    return {"gates": gates, "inputs": inputs_list, "outputs": outputs_list}

# ==============================
# CLI Entry Point
# ==============================
def main():
    parser = argparse.ArgumentParser(description="Generic LLM JSON-to-JSON Processor")
    parser.add_argument("input_file", help="Path to the input JSON file")
    parser.add_argument("--prompt-file", help="Path to a custom system prompt text file (optional)")
    parser.add_argument("--model", default="llama-3.3-70b-versatile", help="Groq Model ID to use")
    parser.add_argument("--skip-yosys", action="store_true", help="Skip Yosys compression, pass raw JSON to LLM")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO, 
        format='%(levelname)s: %(message)s'
    )

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        logging.error("GROQ_API_KEY environment variable is not set.")
        sys.exit(1)

    # Load Custom or Default Prompt
    system_prompt = DEFAULT_SYSTEM_PROMPT
    if args.prompt_file:
        try:
            with open(args.prompt_file, 'r') as pf:
                system_prompt = pf.read()
                logging.info(f"Loaded custom prompt from {args.prompt_file}")
        except Exception as e:
            logging.error(f"Failed to load prompt file: {e}")
            sys.exit(1)

    # Load Input Data
    try:
        with open(args.input_file, 'r') as f:
            input_data = json.load(f)
    except Exception as e:
        logging.error(f"Failed to load input file: {e}")
        sys.exit(1)

    # Pre-process if needed
    if not args.skip_yosys:
        logging.info("Attempting to compress Yosys format...")
        input_data = compress_yosys_json(input_data)

    # Process via LLM Agent
    try:
        agent = GenericJSONAgent(model_id=args.model, api_key=api_key)
        result = agent.process(input_data=input_data, system_prompt=system_prompt)
        
        # Ensure result is a dictionary to prevent '.get()' errors
        if not isinstance(result, dict):
            logging.error("The LLM completely failed to return a JSON object.")
            sys.exit(1)
        
        # ==========================================
        # THE ENFORCER V2: Absolute Mathematical Control
        # ==========================================
        reasoning = result.get("reasoning", "")

        # 1. Gather EVERY single net from the circuit to create a flawless baseline
        all_nets = set()
        
        # Safely extract all inputs and outputs
        if isinstance(input_data, dict):
            for n in input_data.get("inputs", []): all_nets.add(str(n))
            for n in input_data.get("outputs", []): all_nets.add(str(n))
            for g in input_data.get("gates", []):
                if isinstance(g, dict):
                    if g.get("out") is not None: all_nets.add(str(g["out"]))
                    for inp in g.get("in", []): all_nets.add(str(inp))
            
        # 2. Extract dropped faults exclusively from the LLM's text reasoning block
        dropped_matches = re.findall(r'([a-zA-Z0-9_]+)@([01])', reasoning)
        dropped_set = set(f"{net}@{sa}" for net, sa in dropped_matches)

        # 3. Build the final array strictly in Python, ignoring what the LLM tried to put there
        cleaned_list = []
        # Sort nets numerically if possible for cleaner output
        sorted_nets = sorted(list(all_nets), key=lambda x: int(x) if x.isdigit() else x)
        
        for net_str in sorted_nets:
            for sa_str in ["0", "1"]:
                # Only keep the fault if the LLM did NOT explicitly drop it
                if f"{net_str}@{sa_str}" not in dropped_set:
                    # Convert back to int if it was originally an int
                    net_val = int(net_str) if net_str.isdigit() else net_str
                    cleaned_list.append({"net": net_val, "sa": int(sa_str)})

        # 4. Overwrite the LLM's broken output with our perfect array
        result["collapsed_fault_list"] = cleaned_list
        result["_python_enforcer_"] = f"Started with {len(all_nets)*2} total faults. LLM reasoning dropped {len(dropped_set)}. Final list has {len(cleaned_list)}."
        # ==========================================

        # Print final resulting JSON to stdout
        print(json.dumps(result, indent=2))
        
    except Exception as e:
        logging.error(f"Processing failed: {e}")
        # Print the exact line where the error occurred for easier debugging
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()