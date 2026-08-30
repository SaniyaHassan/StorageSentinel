#!/usr/bin/env bash
set -euo pipefail

# StorageSentinel - Shell-first CLI for disk scanning and cleanup
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
CONFIG_FILE="$SCRIPT_DIR/config.conf"
PENDING_FILE="$SCRIPT_DIR/pending_actions.csv"
HISTORY_FILE="$SCRIPT_DIR/history.log"

# Default configuration values
SCAN_ROOT="/home"
ALERT_WARNING_PCT=80
ALERT_CRITICAL_PCT=90
ALERT_EMERGENCY_PCT=95
LARGE_FILE_GB=5
MIN_DIR_SIZE_GB=1
TRASH_MAX_AGE_DAYS=30
DUPLICATE_MIN_SIZE_MB=50
CLEAN_CONDA_CACHE=1
CLEAN_PIP_CACHE=1
CLEAN_JOURNALD_LOGS=1
JOURNALD_MAX_AGE_DAYS=30
COLD_DATA_DAYS=180
DEFAULT_QUOTA_GB=150.0
PROTECTED_PATHS=("/var/lib/postgresql" "/var/lib/mysql" "/etc" "/boot")
PROTECTED_EXTENSIONS=(".db" ".sqlite" ".sqlite3")
ENABLE_EMAIL=0
SMTP_SERVER="localhost"
SMTP_PORT=25
FROM_ADDRESS="sentinel@yourdomain.com"
TO_ADDRESSES=("admin@yourdomain.com")

if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$CONFIG_FILE"
fi

function is_protected_path() {
  local path="$1"
  for protected in "${PROTECTED_PATHS[@]}"; do
    if [[ "$path" == "$protected" || "$path" == "$protected"/* ]]; then
      return 0
    fi
  done
  return 1
}

function is_protected_extension() {
  local path="$1"
  for ext in "${PROTECTED_EXTENSIONS[@]}"; do
    if [[ "${path,,}" == *"$ext" ]]; then
      return 0
    fi
  done
  return 1
}

function write_pending_header() {
  echo "id|action_type|target_path|size_gb|risk|approved|executed|description" > "$PENDING_FILE"
}

function format_gb() {
  awk "BEGIN {printf \"%.2f\", $1 / 1024 / 1024 / 1024}"
}

declare -a AUTO_ACTIONS=()
declare -a MANUAL_ACTIONS=()
declare -a DUPLICATE_SUMMARIES=()
declare DUPLICATE_TOTAL_SAVINGS=0.0

declare -A FILE_TYPE_SIZES=()

declare -a COLD_DIRECTORIES=()

declare -a LARGE_FILE_ENTRIES=()

function format_pct() {
  awk "BEGIN {printf \"%.2f\", $1}"
}

function get_disk_usage() {
  local path="$1"
  local line total used free percent
  line=$(df --output=size,used,avail,pcent --block-size=1 "$path" 2>/dev/null | tail -n 1)
  if [[ -z "$line" ]]; then
    printf "0 0 0 0"
    return
  fi
  read -r total used free percent < <(printf '%s' "$line") || true
  percent=${percent%%%}
  printf "%s %s %s %s" "$total" "$used" "$free" "$percent"
}

# --- Live system health snapshot (CPU/load/RAM/swap/uptime) -------------
# Self-contained: reads /proc directly, no sqlite3 or server_health.db
# dependency. Used by `report` to show current system health alongside
# storage usage.

function read_cpu_times() {
  local user nice system idle iowait irq softirq steal guest guest_nice total idle_sum
  read -r _ user nice system idle iowait irq softirq steal guest guest_nice < /proc/stat
  total=$((user + nice + system + idle + iowait + irq + softirq + steal + guest + guest_nice))
  idle_sum=$((idle + iowait))
  printf "%s %s" "$total" "$idle_sum"
}

function get_cpu_percent() {
  local total1 idle1 total2 idle2 busy delta
  read -r total1 idle1 < <(read_cpu_times) || true
  sleep 0.2
  read -r total2 idle2 < <(read_cpu_times) || true
  busy=$(( (total2 - total1) - (idle2 - idle1) ))
  delta=$(( total2 - total1 ))
  if (( delta <= 0 )); then
    printf "0.0"
    return
  fi
  awk -v busy="$busy" -v total="$delta" 'BEGIN {printf "%.1f", 100 * busy / total}'
}

function get_load_average() {
  local load1 load5 load15
  read -r load1 load5 load15 _ < /proc/loadavg
  printf "%.2f %.2f %.2f" "$load1" "$load5" "$load15"
}

function get_memory_metrics() {
  local memtotal=0 memfree=0 buffers=0 cached=0 available=0 swap_total=0 swap_free=0
  while read -r key value _; do
    case "$key" in
      MemTotal:) memtotal=$value ;;
      MemFree:) memfree=$value ;;
      Buffers:) buffers=$value ;;
      Cached:) cached=$value ;;
      MemAvailable:) available=$value ;;
      SwapTotal:) swap_total=$value ;;
      SwapFree:) swap_free=$value ;;
    esac
  done < /proc/meminfo

  if (( available == 0 )); then
    available=$((memfree + buffers + cached))
  fi
  local used=$((memtotal - available))
  awk -v total="$memtotal" -v used="$used" -v free="$available" -v swap_total="$swap_total" -v swap_used="$((swap_total - swap_free))" 'BEGIN {printf "%.2f %.2f %.2f %.2f %.2f", total/1024/1024, used/1024/1024, free/1024/1024, swap_total/1024/1024, swap_used/1024/1024}'
}

function get_uptime_seconds() {
  local uptime
  read -r uptime _ < /proc/uptime
  printf "%.0f" "$uptime"
}

function format_uptime() {
  local seconds="$1"
  local days hours minutes
  days=$((seconds / 86400))
  hours=$(((seconds % 86400) / 3600))
  minutes=$(((seconds % 3600) / 60))
  printf "%dd %dh %dm" "$days" "$hours" "$minutes"
}

function get_owner() {
  local file="$1"
  local owner
  owner=$(stat -c '%U' "$file" 2>/dev/null || true)
  if [[ -z "$owner" ]]; then
    owner=$(stat -c '%u' "$file" 2>/dev/null || echo "unknown")
  fi
  echo "$owner"
}

function path_category() {
  local file="$1"
  local lower="${file,,}"

  if [[ "$lower" == *"/.local/share/trash"* || "$lower" == */trash/* ]]; then
    echo "Trash"
    return
  fi
  if [[ "$lower" == *"/.cache/pip"* || "$lower" == */pip/cache* || "$lower" == *"/pkgs"* || "$lower" == *"/cache/"* ]]; then
    echo "Caches"
    return
  fi
  local ext=".${file##*.}"
  ext="${ext,,}"
  case "$ext" in
    .mp4|.mkv|.avi|.mov|.flv|.webm|.mpeg|.mpg)
      echo "Videos"
      ;;
    .iso|.img|.dmg)
      echo "ISOs"
      ;;
    .bin|.pt|.pth|.ckpt|.safetensors|.onnx|.gguf|.h5|.model|.weights)
      echo "AI Models"
      ;;
    .db|.sqlite|.sqlite3)
      echo "Databases"
      ;;
    .csv|.tsv|.json|.parquet|.hdf5|.npz|.npy|.xml)
      echo "Datasets"
      ;;
    *)
      echo "Other"
      ;;
  esac
}

function truncate_path() {
  local path="$1"
  if [[ "${#path}" -le 50 ]]; then
    printf "%s" "$path"
  else
    printf "...%s" "${path: -47}"
  fi
}

function write_pending_header() {
  echo "id|action_type|target_path|size_gb|risk|approved|executed|description" > "$PENDING_FILE"
}

function log_pending_action() {
  local id="$1"
  local action="$2"
  local target="$3"
  local size="$4"
  local risk="$5"
  local approved="$6"
  local executed="$7"
  local description="$8"

  echo "$id|$action|$target|$size|$risk|$approved|$executed|$description" >> "$PENDING_FILE"
  printf '  Recommendation #%s: action=%s path=%s size=%sGB risk=%s - %s\n' \
    "$id" "$action" "$target" "$size" "$risk" "$description"
}

function detect_duplicates() {
  local dup_file="$1"
  local min_size_mb="$2"
  local min_bytes=$((min_size_mb * 1024 * 1024))
  local size
  local path
  local sorted_file
  sorted_file=$(mktemp)

  sort -z -k1,1n "$dup_file" > "$sorted_file"

  local current_size=""
  local -a current_paths=()
  while IFS=$'\t' read -r -d '' size path; do
    if [[ "$size" != "$current_size" && ${#current_paths[@]} -gt 1 ]]; then
      _process_duplicate_group "$current_size" "${current_paths[@]}"
      current_paths=()
    fi
    current_size="$size"
    current_paths+=("$path")
  done < "$sorted_file"

  if [[ ${#current_paths[@]} -gt 1 ]]; then
    _process_duplicate_group "$current_size" "${current_paths[@]}"
  fi

  rm -f "$sorted_file"
}

function _process_duplicate_group() {
  local size_bytes="$1"
  shift
  local -a paths=("$@")
  local partial_file full_file
  partial_file=$(mktemp)
  full_file=$(mktemp)

  for path in "${paths[@]}"; do
    local p_hash
    p_hash=$(head -c 1048576 "$path" 2>/dev/null | sha256sum 2>/dev/null | awk '{print $1}')
    if [[ -n "$p_hash" ]]; then
      printf '%s\t%s\0' "$p_hash" "$path" >> "$partial_file"
    fi
  done

  sort -z "$partial_file" > "${partial_file}.sorted"
  local current_partial=""
  local -a group_paths=()

  while IFS=$'\t' read -r -d '' grouping path; do
    if [[ "$grouping" != "$current_partial" && ${#group_paths[@]} -gt 1 ]]; then
      _process_full_hash_group "$size_bytes" "${group_paths[@]}"
      group_paths=()
    fi
    current_partial="$grouping"
    group_paths+=("$path")
  done < "${partial_file}.sorted"
  if [[ ${#group_paths[@]} -gt 1 ]]; then
    _process_full_hash_group "$size_bytes" "${group_paths[@]}"
  fi

  rm -f "$partial_file" "${partial_file}.sorted"
}

function _process_full_hash_group() {
  local size_bytes="$1"
  shift
  local -a paths=("$@")
  local full_file
  full_file=$(mktemp)
  local full_hash

  for path in "${paths[@]}"; do
    full_hash=$(sha256sum "$path" 2>/dev/null | awk '{print $1}')
    if [[ -n "$full_hash" ]]; then
      printf '%s\t%s\0' "$full_hash" "$path" >> "$full_file"
    fi
  done

  sort -z "$full_file" > "${full_file}.sorted"
  local last_hash=""
  local -a group_paths=()
  local path

  while IFS=$'\t' read -r -d '' current_hash path; do
    if [[ "$current_hash" != "$last_hash" && ${#group_paths[@]} -gt 1 ]]; then
      _record_duplicate_group "$size_bytes" "${group_paths[@]}"
      group_paths=()
    fi
    last_hash="$current_hash"
    group_paths+=("$path")
  done < "${full_file}.sorted"

  if [[ ${#group_paths[@]} -gt 1 ]]; then
    _record_duplicate_group "$size_bytes" "${group_paths[@]}"
  fi

  rm -f "$full_file" "${full_file}.sorted"
}

function _record_duplicate_group() {
  local size_bytes="$1"
  shift
  local -a paths=("$@")
  local size_gb
  size_gb=$(format_gb "$size_bytes")
  local original="${paths[0]}"
  local original_owner
  original_owner=$(get_owner "$original")

  local summary="  $size_gb        Original: $(basename "$original") (Owner: $original_owner)"
  local duplicates_count=0
  for ((i=1; i<${#paths[@]}; i++)); do
    local duplicate="${paths[i]}"
    local owner
    owner=$(get_owner "$duplicate")
    summary+=$'\n'
    summary+="               -> Duplicate: $duplicate (Owner: $owner)"

    local description="Delete duplicate file $duplicate and keep original $original"
    MANUAL_ACTIONS+=("delete|$duplicate|$size_gb|Medium|$description")
    duplicates_count=$((duplicates_count + 1))
  done

  DUPLICATE_SUMMARIES+=("$summary")
  local savings
  savings=$(awk "BEGIN {printf \"%.3f\", $size_gb * $duplicates_count}")
  DUPLICATE_TOTAL_SAVINGS=$(awk "BEGIN {printf \"%.3f\", $DUPLICATE_TOTAL_SAVINGS + $savings}")
}

function ensure_numeric() {
  awk "BEGIN {printf \"%.2f\", $1}"
}

function numeric_ge() {
  awk "BEGIN {exit !($1 >= $2)}"
}

function validate_root() {
  local root="$1"
  if [[ ! -d "$root" ]]; then
    echo "Error: path does not exist: $root"
    exit 1
  fi
}

# Shared filesystem walk used by both `report` and `scan`. Populates the
# global state arrays/temp files declared above (USER_METRICS_FILE,
# TRASH_FILE, CONDA_FILE, PIP_FILE, FILE_TYPE_SIZES, LARGE_FILE_ENTRIES,
# DUPLICATE_SUMMARIES, DUPLICATE_TOTAL_SAVINGS, COLD_DIRECTORIES, and the
# disk usage globals TOTAL_GB/USED_GB/FREE_GB/PERCENT_USED).
function collect_scan_data() {
  local root="$1"

  local total used free percent
  read -r total used free percent < <(get_disk_usage "$root") || true
  TOTAL_GB=$(format_gb "$total")
  USED_GB=$(format_gb "$used")
  FREE_GB=$(format_gb "$free")
  PERCENT_USED=$(format_pct "$percent")

  # Reset state
  AUTO_ACTIONS=()
  MANUAL_ACTIONS=()
  DUPLICATE_SUMMARIES=()
  DUPLICATE_TOTAL_SAVINGS=0.0
  LARGE_FILE_ENTRIES=()
  FILE_TYPE_SIZES=( [Videos]=0 [ISOs]=0 [AI Models]=0 [Trash]=0 [Datasets]=0 [Caches]=0 [Databases]=0 [Other]=0 )
  COLD_DIRECTORIES=()

  USER_METRICS_FILE=$(mktemp)
  TRASH_FILE=$(mktemp)
  CONDA_FILE=$(mktemp)
  PIP_FILE=$(mktemp)
  DUP_FILE=$(mktemp)

  echo "Discovering per-user storage usage..."
  while IFS= read -r -d '' userdir; do
    if is_protected_path "$userdir"; then
      continue
    fi
    local username bytes
    username=$(basename "$userdir")
    bytes=$(du -sb "$userdir" 2>/dev/null | cut -f1 || echo 0)
    printf '%s\t%s\n' "$username" "${bytes:-0}" >> "$USER_METRICS_FILE"
  done < <(find "$root" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null)

  echo "Scanning filesystem and categorizing files..."
  while IFS= read -r -d '' file; do
    if is_protected_path "$file" || is_protected_extension "$file"; then
      continue
    fi

    local bytes
    bytes=$(stat -c%s "$file" 2>/dev/null || echo 0)
    local category
    category=$(path_category "$file")
    FILE_TYPE_SIZES["$category"]=$((FILE_TYPE_SIZES["$category"] + bytes))

    if [[ "$bytes" -ge $((LARGE_FILE_GB * 1024 * 1024 * 1024)) ]]; then
      local owner
      owner=$(get_owner "$file")
      printf '%s\t%s\t%s\0' "$bytes" "$owner" "$file" >> "$DUP_FILE" # reuse for duplicates list too
      LARGE_FILE_ENTRIES+=("$bytes|$owner|$file")
    elif [[ "$bytes" -ge $((50 * 1024 * 1024)) ]]; then
      printf '%s\t%s\0' "$bytes" "$file" >> "$DUP_FILE"
    fi
  done < <(find "$root" \( -path "/proc" -o -path "/sys" -o -path "/dev" -o -path "/run" -o -path "/tmp" -o -path "/var/lib/docker" \) -prune -o -type f -print0 2>/dev/null)

  echo "Scanning for trash and cache directories..."
  while IFS= read -r -d '' dir; do
    if is_protected_path "$dir"; then
      continue
    fi
    local bytes
    bytes=$(du -sb "$dir" 2>/dev/null | cut -f1 || echo 0)
    if [[ "$bytes" -gt 0 ]]; then
      printf '%s\t%s\n' "$bytes" "$dir" >> "$TRASH_FILE"
    fi
  done < <(find "$root" \( -path "*/.local/share/Trash" -o -name Trash \) -type d -print0 2>/dev/null)

  if [[ "$CLEAN_CONDA_CACHE" -eq 1 ]]; then
    while IFS= read -r -d '' dir; do
      if is_protected_path "$dir"; then
        continue
      fi
      local bytes
      bytes=$(du -sb "$dir" 2>/dev/null | cut -f1 || echo 0)
      if [[ "$bytes" -gt 0 ]]; then
        printf '%s\t%s\n' "$bytes" "$dir" >> "$CONDA_FILE"
      fi
    done < <(find "$root" -type d \( -path "*/pkgs" -o -path "*/anaconda/*/pkgs" -o -path "*/miniconda3/*/pkgs" \) -print0 2>/dev/null)
  fi

  if [[ "$CLEAN_PIP_CACHE" -eq 1 ]]; then
    while IFS= read -r -d '' dir; do
      if is_protected_path "$dir"; then
        continue
      fi
      local bytes
      bytes=$(du -sb "$dir" 2>/dev/null | cut -f1 || echo 0)
      if [[ "$bytes" -gt 0 ]]; then
        printf '%s\t%s\n' "$bytes" "$dir" >> "$PIP_FILE"
      fi
    done < <(find "$root" -type d \( -path "*/.cache/pip" -o -path "*/pip/cache" \) -print0 2>/dev/null)
  fi

  if [[ "$DUPLICATE_MIN_SIZE_MB" -gt 0 ]]; then
    echo "Scanning for duplicate files (>= ${DUPLICATE_MIN_SIZE_MB} MB)..."
    detect_duplicates "$DUP_FILE" "$DUPLICATE_MIN_SIZE_MB"
  fi

  echo "Scanning for cold directories..."
  while IFS= read -r -d '' dir; do
    if is_protected_path "$dir"; then
      continue
    fi
    local bytes
    bytes=$(du -sb "$dir" 2>/dev/null | cut -f1 || echo 0)
    if [[ "$bytes" -lt $((MIN_DIR_SIZE_GB * 1024 * 1024 * 1024)) ]]; then
      continue
    fi
    local days_inactive
    days_inactive=$(printf '%s' "$(( ( $(date +%s) - $(stat -c %Y "$dir" 2>/dev/null || echo 0) ) / 86400 ))")
    if [[ "$days_inactive" -ge "$COLD_DATA_DAYS" ]]; then
      COLD_DIRECTORIES+=("$bytes|$days_inactive|$dir")
    fi
  done < <(find "$root" -type d -mtime +${COLD_DATA_DAYS} -print0 2>/dev/null)
}

function cleanup_scan_tempfiles() {
  rm -f "$USER_METRICS_FILE" "$TRASH_FILE" "$CONDA_FILE" "$PIP_FILE" "$DUP_FILE"
}

# Prints the read-only storage report (sections 1-5). Assumes
# collect_scan_data has already populated the relevant globals.
function print_storage_report() {
  local root="$1"

  echo
  echo "============================================================"
  echo "                 STORAGESENTINEL STORAGE REPORT"
  echo "============================================================"

  echo
  echo "### 1. System Health Snapshot"
  echo "  CPU Usage:    ${SNAPSHOT_CPU_PCT}%"
  echo "  Load Average: ${SNAPSHOT_LOAD1} (1m)  ${SNAPSHOT_LOAD5} (5m)  ${SNAPSHOT_LOAD15} (15m)"
  echo "  RAM Usage:    ${SNAPSHOT_RAM_USED} GB / ${SNAPSHOT_RAM_TOTAL} GB"
  echo "  Swap Usage:   ${SNAPSHOT_SWAP_USED} GB / ${SNAPSHOT_SWAP_TOTAL} GB"
  echo "  Uptime:       ${SNAPSHOT_UPTIME_DISPLAY}"

  echo
  echo "### 2. Filesystem Utilization"
  echo "  Disk Usage: ${USED_GB} GB / ${TOTAL_GB} GB"
  echo "  Available:  ${FREE_GB} GB"
  echo "  Utilization: ${PERCENT_USED}%"
  if numeric_ge "$PERCENT_USED" "$ALERT_CRITICAL_PCT"; then
    echo
    echo "  ALERT STATUS:"
    echo "  [!] CRITICAL WARNING: Disk usage is at ${PERCENT_USED}% (>= ${ALERT_CRITICAL_PCT}%)!"
  elif numeric_ge "$PERCENT_USED" "$ALERT_WARNING_PCT"; then
    echo
    echo "  ALERT STATUS:"
    echo "  [!] WARNING: Disk usage is at ${PERCENT_USED}% (>= ${ALERT_WARNING_PCT}%)!"
  fi

  echo
  echo "### 3. User Storage & Quotas"
  echo "  User            Usage (GB)   Quota (GB)   Status      "
  echo "  -------------------------------------------------------"
  if [[ -s "$USER_METRICS_FILE" ]]; then
    sort -t$'\t' -k2,2nr "$USER_METRICS_FILE" | while IFS=$'\t' read -r user bytes; do
      local usage_gb quota status
      usage_gb=$(format_gb "$bytes")
      quota="$DEFAULT_QUOTA_GB"
      if awk "BEGIN {exit !($usage_gb > $quota)}"; then
        status="Exceeded"
      else
        status="OK"
      fi
      printf "  %-15s %-12s %-12s %-12s\n" "$user" "$usage_gb" "$quota" "$status"
    done
  else
    echo "  No users discovered under $root"
  fi

  echo
  echo "### 4. File Type Analytics"
  echo "  Category        Size (GB)  "
  echo "  ----------------------------"
  for category in "AI Models" "ISOs" "Trash" "Datasets" "Caches" "Videos" "Databases" "Other"; do
    local cat_bytes
    cat_bytes=${FILE_TYPE_SIZES[$category]:-0}
    if [[ "$cat_bytes" -gt 0 ]]; then
      printf "  %-15s %-12s\n" "$category" "$(format_gb "$cat_bytes")"
    fi
  done

  echo
  echo "### 5. Large Files (>${LARGE_FILE_GB} GB)"
  echo "  Owner        Size (GB)    Path                                              "
  echo "  --------------------------------------------------------------------------------"
  local count=0
  for entry in "${LARGE_FILE_ENTRIES[@]}"; do
    if [[ "$count" -ge 15 ]]; then
      break
    fi
    IFS='|' read -r bytes owner file <<< "$entry"
    local size_gb path_display
    size_gb=$(format_gb "$bytes")
    path_display=$(truncate_path "$file")
    printf "  %-12s %-12s %-50s\n" "$owner" "$size_gb" "$path_display"
    count=$((count + 1))
  done
  if [[ ${#LARGE_FILE_ENTRIES[@]} -gt 15 ]]; then
    echo "  ... and $(( ${#LARGE_FILE_ENTRIES[@]} - 15)) more large files."
  fi

  echo
  echo "### 6. Duplicate File Summary"
  if [[ ${#DUPLICATE_SUMMARIES[@]} -gt 0 ]]; then
    echo "  Size (GB)    Duplicate File Group                                        "
    echo "  --------------------------------------------------------------------------------"
    for summary in "${DUPLICATE_SUMMARIES[@]}"; do
      printf "%s\n" "$summary"
    done
    echo
    echo "  Potential savings from duplicate removal: ${DUPLICATE_TOTAL_SAVINGS} GB"
  else
    echo "  No large duplicates found."
  fi
  echo
}

# `report` - read-only. Walks the filesystem and prints the detailed
# storage report. Does not touch pending_actions.csv or history.log.
# Collects the live system health snapshot into the SNAPSHOT_* globals
# used by print_storage_report.
function collect_health_snapshot() {
  local uptime_seconds
  SNAPSHOT_CPU_PCT=$(get_cpu_percent)
  read -r SNAPSHOT_LOAD1 SNAPSHOT_LOAD5 SNAPSHOT_LOAD15 < <(get_load_average) || true
  read -r SNAPSHOT_RAM_TOTAL SNAPSHOT_RAM_USED _ SNAPSHOT_SWAP_TOTAL SNAPSHOT_SWAP_USED < <(get_memory_metrics) || true
  uptime_seconds=$(get_uptime_seconds)
  SNAPSHOT_UPTIME_DISPLAY=$(format_uptime "$uptime_seconds")
}

function report() {
  local root="${1:-$SCAN_ROOT}"
  validate_root "$root"
  echo "Generating storage report for: $root"

  collect_health_snapshot
  collect_scan_data "$root"
  print_storage_report "$root"

  cleanup_scan_tempfiles
}

# Turns the data collected by collect_scan_data into AUTO_ACTIONS (safe,
# pre-approved) and MANUAL_ACTIONS (require ./sentinel.sh approve).
function build_candidates() {
  local high_usage=0
  if numeric_ge "$PERCENT_USED" "$ALERT_WARNING_PCT"; then
    high_usage=1
  fi

  if [[ -s "$TRASH_FILE" ]]; then
    while IFS=$'\t' read -r bytes dir; do
      local size_gb
      size_gb=$(format_gb "$bytes")
      if [[ "$high_usage" -eq 1 ]]; then
        AUTO_ACTIONS+=("empty_trash|$dir|$size_gb|Low|Empty trash directory for user at $dir")
      else
        MANUAL_ACTIONS+=("empty_trash|$dir|$size_gb|Low|Empty trash directory for user at $dir")
      fi
    done < "$TRASH_FILE"
  fi

  if [[ "$CLEAN_JOURNALD_LOGS" -eq 1 ]]; then
    local journald_description="Vacuum systemd journald logs to keep last ${JOURNALD_MAX_AGE_DAYS} days"
    if [[ "$high_usage" -eq 1 ]]; then
      AUTO_ACTIONS+=("journald_clean|journald|0.50|Low|$journald_description")
    else
      MANUAL_ACTIONS+=("journald_clean|journald|0.50|Low|$journald_description")
    fi
  fi

  if [[ -s "$CONDA_FILE" ]]; then
    while IFS=$'\t' read -r bytes dir; do
      local size_gb owner
      size_gb=$(format_gb "$bytes")
      owner=$(get_owner "$dir")
      MANUAL_ACTIONS+=("conda_clean|$dir|$size_gb|Medium|Clean conda package cache for user $owner at $dir")
    done < "$CONDA_FILE"
  fi

  if [[ -s "$PIP_FILE" ]]; then
    while IFS=$'\t' read -r bytes dir; do
      local size_gb owner
      size_gb=$(format_gb "$bytes")
      owner=$(get_owner "$dir")
      MANUAL_ACTIONS+=("pip_clean|$dir|$size_gb|Medium|Clean pip cache for user $owner at $dir")
    done < "$PIP_FILE"
  fi

  if [[ ${#COLD_DIRECTORIES[@]} -gt 0 ]]; then
    for entry in "${COLD_DIRECTORIES[@]}"; do
      local bytes days dir size_gb
      IFS='|' read -r bytes days dir <<< "$entry"
      size_gb=$(format_gb "$bytes")
      MANUAL_ACTIONS+=("compress|$dir|$size_gb|Medium|Compress cold directory (inactive for ${days} days)")
    done
  fi
}

function print_candidate_summary() {
  local auto_total=0.0
  local manual_total=0.0
  for action_entry in "${AUTO_ACTIONS[@]}"; do
    IFS='|' read -r _ _ size _ _ <<< "$action_entry"
    auto_total=$(awk "BEGIN {printf \"%.3f\", $auto_total + $size}")
  done
  for action_entry in "${MANUAL_ACTIONS[@]}"; do
    IFS='|' read -r _ _ size _ _ <<< "$action_entry"
    manual_total=$(awk "BEGIN {printf \"%.3f\", $manual_total + $size}")
  done

  echo
  echo "============================================================"
  echo "               STORAGESENTINEL CLEANUP CANDIDATES"
  echo "============================================================"

  echo
  echo "  [A] Safe Auto-Clean Actions (Potential Recovery: ${auto_total} GB)"
  echo "  --------------------------------------------------------------------------------"
  if [[ ${#AUTO_ACTIONS[@]} -gt 0 ]]; then
    for action_entry in "${AUTO_ACTIONS[@]}"; do
      IFS='|' read -r action target size risk description <<< "$action_entry"
      printf "  - [%s] %s (%s GB)\n" "$risk" "$description" "$size"
    done
  else
    echo "  None pending."
  fi

  echo
  echo "  [B] Manual Approval Needed (Potential Recovery: ${manual_total} GB)"
  echo "  --------------------------------------------------------------------------------"
  if [[ ${#MANUAL_ACTIONS[@]} -gt 0 ]]; then
    for action_entry in "${MANUAL_ACTIONS[@]}"; do
      IFS='|' read -r action target size risk description <<< "$action_entry"
      printf "  - [%s] %s\n" "$risk" "$description"
      printf "    Target: %s\n" "$target"
    done
  else
    echo "  None pending."
  fi
  echo
}

# `scan` - finds cleanup candidates (trash, caches, journald, duplicates,
# cold directories) and writes them to pending_actions.csv for review via
# `./sentinel.sh approve` and execution via `./sentinel.sh clean`.
function scan() {
  local root="${1:-$SCAN_ROOT}"
  validate_root "$root"
  echo "Starting scan of: $root"

  collect_scan_data "$root"
  build_candidates

  write_pending_header
  local id=1
  for action_entry in "${AUTO_ACTIONS[@]}"; do
    IFS='|' read -r action target size risk description <<< "$action_entry"
    log_pending_action "$id" "$action" "$target" "$size" "$risk" "1" "0" "$description"
    id=$((id + 1))
  done
  for action_entry in "${MANUAL_ACTIONS[@]}"; do
    IFS='|' read -r action target size risk description <<< "$action_entry"
    log_pending_action "$id" "$action" "$target" "$size" "$risk" "0" "0" "$description"
    id=$((id + 1))
  done

  print_candidate_summary

  echo "Scan completed. Pending actions written to $PENDING_FILE"
  echo "$(date '+%Y-%m-%d %H:%M:%S') - scan root=$root - actions=$(( ${#AUTO_ACTIONS[@]} + ${#MANUAL_ACTIONS[@]} ))" >> "$HISTORY_FILE"

  cleanup_scan_tempfiles
}

function approve() {
  if [[ ! -f "$PENDING_FILE" ]]; then
    echo "No pending actions found. Run './sentinel.sh scan' first."
    return
  fi

  local tmpfile
  tmpfile="$(mktemp)"
  IFS='\n' read -r header < "$PENDING_FILE"
  echo "$header" > "$tmpfile"

  tail -n +2 "$PENDING_FILE" | while IFS='|' read -r id action path size risk approved executed description; do
    echo
    echo "ID: $id"
    echo "Action: $action"
    echo "Path: $path"
    echo "Size: ${size}GB"
    echo "Risk: $risk"
    echo "Description: $description"
    echo "Approved: $approved"
    local choice=""
    while true; do
      read -rn1 -p "Approve this action? [y/n/s/q]: " choice
      echo
      case "$choice" in
        y)
          approved=1
          break
          ;;
        n)
          approved=0
          break
          ;;
        s)
          break
          ;;
        q)
          echo "Saving decisions and exiting."
          echo "$id|$action|$path|$size|$risk|$approved|$executed|$description" >> "$tmpfile"
          awk -F'|' -v start="$id" 'NR>1 && $1 >= start {print}' "$PENDING_FILE" >> "$tmpfile"
          mv "$tmpfile" "$PENDING_FILE"
          return
          ;;
        *)
          echo "Please enter y, n, s, or q."
          ;;
      esac
    done
    echo "$id|$action|$path|$size|$risk|$approved|$executed|$description" >> "$tmpfile"
  done

  mv "$tmpfile" "$PENDING_FILE"
  echo "Pending actions updated."
}

function clean_actions() {
  if [[ ! -f "$PENDING_FILE" ]]; then
    echo "No pending actions found. Run './sentinel.sh scan' and './sentinel.sh approve' first."
    return
  fi

  local tmpfile
  tmpfile="$(mktemp)"
  IFS='\n' read -r header < "$PENDING_FILE"
  echo "$header" > "$tmpfile"

  tail -n +2 "$PENDING_FILE" | while IFS='|' read -r id action path size risk approved executed description; do
    if [[ "$approved" != "1" || "$executed" == "1" ]]; then
      echo "$id|$action|$path|$size|$risk|$approved|$executed|$description" >> "$tmpfile"
      continue
    fi

    if [[ ! -e "$path" ]]; then
      echo "Skipping missing path: $path"
      executed=1
      echo "$id|$action|$path|$size|$risk|$approved|$executed|$description" >> "$tmpfile"
      continue
    fi

    case "$action" in
      empty_trash)
        echo "Executing empty_trash on $path"
        if [[ -d "$path" ]]; then
          find "$path" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
        fi
        executed=1
        ;;
      conda_clean|pip_clean|delete)
        echo "Executing $action on $path"
        rm -rf "$path"
        executed=1
        ;;
      journald_clean)
        echo "Executing journald_clean"
        if command -v journalctl >/dev/null 2>&1; then
          journalctl --vacuum-time="${JOURNALD_MAX_AGE_DAYS}d" 2>/dev/null || true
        fi
        executed=1
        ;;
      compress)
        echo "Compress action approved but not automatically executed for $path"
        executed=0
        ;;
      *)
        echo "Unknown action type: $action"
        executed=0
        ;;
    esac
    echo "$id|$action|$path|$size|$risk|$approved|$executed|$description" >> "$tmpfile"
  done

  mv "$tmpfile" "$PENDING_FILE"
  echo "Cleanup complete. Approved actions updated in $PENDING_FILE."
}

function history() {
  if [[ ! -f "$HISTORY_FILE" ]]; then
    echo "No history available yet. Run './sentinel.sh scan' first."
    return
  fi
  cat "$HISTORY_FILE"
}

function help_text() {
  cat <<'EOF'
StorageSentinel shell CLI

Usage:
  ./sentinel.sh report [root]  - Generate a read-only storage report (usage, users, file types, large files, duplicates)
  ./sentinel.sh scan [root]    - Find cleanup candidates and write pending_actions.csv
  ./sentinel.sh approve        - Approve or reject pending actions
  ./sentinel.sh clean          - Execute approved cleanup actions
  ./sentinel.sh history        - Show scan history summary
  ./sentinel.sh help           - Show this help message
EOF
}

case "${1:-help}" in
  report)
    report "${2:-}"
    ;;
  scan)
    scan "${2:-}"
    ;;
  approve)
    approve
    ;;
  clean)
    clean_actions
    ;;
  history)
    history
    ;;
  help|--help|-h)
    help_text
    ;;
  *)
    echo "Unknown command: ${1:-}" >&2
    help_text
    exit 1
    ;;
 esac