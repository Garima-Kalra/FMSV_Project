import json
import copy
import os

with open("gold_netlist.json") as f:
    gold = json.load(f)

module_name = list(gold["modules"].keys())[0]
module = gold["modules"][module_name]

# Extract nets
nets = []
for name, val in module["netnames"].items():
    bits = val["bits"]
    if isinstance(bits[0], int):   # ignore constants
        nets.append(bits[0])

print(nets)

os.makedirs("faults", exist_ok=True)

def inject_fault(net_id, stuck_val):
    faulty = copy.deepcopy(gold)
    module = faulty["modules"][module_name]

    for cell_name, cell_data in module["cells"].items():
        # Look up the directions for this cell type
        directions = cell_data.get("port_directions", {})
        
        for port, conn in cell_data["connections"].items():
            # ONLY inject if the port is an INPUT
            if directions.get(port) == "input":
                new_conn = []
                for bit in conn:
                    if bit == net_id:
                        new_conn.append("0" if stuck_val == 0 else "1")
                    else:
                        new_conn.append(bit)
                cell_data["connections"][port] = new_conn
                
    return faulty

# Generate faults
for net in nets:
    for val in [0, 1]:
        faulty = inject_fault(net, val)
        fname = f"faults/net_{net}_SA{val}.json"
        with open(fname, "w") as f:
            json.dump(faulty, f, indent=2)

print("All faults generated!")

module_name = list(gold["modules"].keys())[0]
netnames = gold["modules"][module_name]["netnames"]

bit_to_name = {}

for name, val in netnames.items():
    bits = val["bits"]
    for b in bits:
        if isinstance(b, int):
            bit_to_name[b] = name

# Save mapping
with open("bit_mapping.json", "w") as f:
    json.dump(bit_to_name, f, indent=2)

print("Mapping saved!")