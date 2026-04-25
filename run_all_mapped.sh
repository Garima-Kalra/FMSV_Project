#!/bin/bash

echo "Starting ATPG Pipeline"

echo "Cleaning up old data..."
rm -rf faults/ logs/ circuit_mapped.png
mkdir -p faults logs

START_TIME=$(date +%s)

yosys gen_gold_mapped.ys

echo "Generating faults..."
python3 generate_faults.py || exit 1

echo "Running SAT..."
python3 run_sat_mapped.py || exit 1

echo "Done. Results saved in mapped_results.csv"

END_TIME=$(date +%s)

TOTAL_TIME=$((END_TIME - START_TIME))

echo ""
echo "Total Execution Time: ${TOTAL_TIME} seconds"