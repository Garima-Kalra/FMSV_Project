#!/bin/bash

echo "Starting ATPG Pipeline"

echo "Cleaning up old data..."
rm -rf faults/ logs/ circuit_mapped.png
mkdir -p faults logs

START_TIME=$(date +%s)

yosys gen_gold_mapped.ys

echo "Generating faults..."
python3 generate_faults.py || exit 1

python3 groq_agent1_2.py 17 1

cd minisat
./build/release/bin/minisat sat.cnf result.txt
./build/release/bin/minisat sat.cnf result.txt partial.txt -partial

END_TIME=$(date +%s)

TOTAL_TIME=$((END_TIME - START_TIME))

echo ""
echo "Total Execution Time: ${TOTAL_TIME} seconds"