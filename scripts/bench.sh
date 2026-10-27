#!/usr/bin/env bash
# Measures Vanta's startup time, idle memory and binary size (Linux).
# Usage: scripts/bench.sh [path/to/vanta] [runs]
# Uses throwaway config/data directories so your real install is untouched.
set -euo pipefail

BIN="${1:-target/release/vanta}"
RUNS="${2:-10}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export XDG_CONFIG_HOME="$TMP/config" XDG_DATA_HOME="$TMP/data"

echo "binary: $BIN"
printf "size: %.2f MB\n" "$(echo "$(stat -c %s "$BIN") / 1048576" | bc -l)"

# Warm-up run (creates config/data dirs, fills the page cache).
VANTA_BENCH_STARTUP=1 "$BIN" >/dev/null 2>&1 || true

frames=()
walls=()
for _ in $(seq "$RUNS"); do
  start=$(date +%s%N)
  out=$(VANTA_BENCH_STARTUP=1 "$BIN" 2>/dev/null)
  end=$(date +%s%N)
  frames+=("$(sed -n 's/^first_frame_ms=//p' <<<"$out")")
  walls+=("$(( (end - start) / 1000000 ))")
done

stats() { sort -n | awk '{a[NR]=$1; s+=$1} END {printf "median %.1f ms, min %.1f ms, max %.1f ms, mean %.1f ms", a[int((NR+1)/2)], a[1], a[NR], s/NR}'; }
echo "first frame (in process): $(printf '%s\n' "${frames[@]}" | stats)"
echo "launch → exit after first frame (wall): $(printf '%s\n' "${walls[@]}" | stats)"

# Idle memory: start normally, let it settle, sample.
"$BIN" >/dev/null 2>&1 &
pid=$!
sleep 8
rss=$(awk '/^VmRSS/ {print $2}' "/proc/$pid/status")
pss=$(awk '/^Pss:/ {print $2}' "/proc/$pid/smaps_rollup")
priv=$(awk '/^Private_(Clean|Dirty):/ {s+=$2} END {print s}' "/proc/$pid/smaps_rollup")
cpu1=$(awk '{print $14+$15}' "/proc/$pid/stat"); sleep 5; cpu2=$(awk '{print $14+$15}' "/proc/$pid/stat")
kill "$pid"; wait "$pid" 2>/dev/null || true
printf "idle memory: RSS %.1f MB, PSS %.1f MB, private %.1f MB\n" "$(echo "$rss/1024" | bc -l)" "$(echo "$pss/1024" | bc -l)" "$(echo "$priv/1024" | bc -l)"
echo "idle CPU over 5 s: $(( cpu2 - cpu1 )) clock ticks"
