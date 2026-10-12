#!/usr/bin/env bash
# Sample whole-machine CPU% and used RAM every 5 s into a CSV until the file STOP exists next to it.
out="${1:-resources.csv}"; echo "time,cpu_busy_pct,mem_used_mb,vina_procs" > "$out"
prev=($(awk '/^cpu /{print $2+$3+$4+$7+$8, $2+$3+$4+$5+$6+$7+$8}' /proc/stat))
while [ ! -e "$out.STOP" ]; do
  sleep 5
  cur=($(awk '/^cpu /{print $2+$3+$4+$7+$8, $2+$3+$4+$5+$6+$7+$8}' /proc/stat))
  busy=$(( (cur[0]-prev[0])*100 / (cur[1]-prev[1]+1) )); prev=("${cur[@]}")
  mem=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print int((t-a)/1024)}' /proc/meminfo)
  echo "$(date +%T),$busy,$mem,$(pgrep -c -f '[v]ina --receptor' || true)" >> "$out"
done
