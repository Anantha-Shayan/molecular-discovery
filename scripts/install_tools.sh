#!/usr/bin/env bash
# Install the BioSolveIT command-line tools and licence into the default locations the app searches,
# then verify each with the vendor's own diagnostics.
#
#   ./scripts/install_tools.sh [SOURCE_DIR]          # default SOURCE_DIR: ~/Desktop/Molecular-Solutions
#
# Tools   -> ${TOOLS_DIR:-~/.local/share/molecular-tools}/<tool>/...      (readable by everyone, so a container can run them)
# Licence -> ${BIOSOLVE_LICENSE_DIR:-~/.config/biosolveit}/*.lic           (0700 dir, 0644 file: container user must read it)
# Neither location is inside the repository. Run again to upgrade; nothing outside these folders is touched.
set -euo pipefail
SRC="${1:-$HOME/Desktop/Molecular-Solutions}"
TOOLS="${TOOLS_DIR:-$HOME/.local/share/molecular-tools}"
LIC="${BIOSOLVE_LICENSE_DIR:-$HOME/.config/biosolveit}"
[ -d "$SRC" ] || { echo "Source folder not found: $SRC" >&2; exit 1; }

mkdir -p "$TOOLS"
shopt -s nullglob
n=0
for tgz in "$SRC"/*.tar.gz; do
  base="$(basename "$tgz" .tar.gz)"; name="${base%%-[0-9]*}"      # flexx-6.5.3-Linux-x64 -> flexx
  top="$(tar tzf "$tgz" | sed -n 1p | cut -d/ -f1)"
  case "$name" in flexx|hydescorer|fastgrow) ;; *) echo "skip $base (not a tool this app uses)"; continue;; esac
  # Guard against the mistake seen once: a tarball that does not contain the expected executable.
  if ! tar tzf "$tgz" | grep -E "(^|/)${name}$" >/dev/null; then echo "!! $base does not contain a '$name' executable; skipping" >&2; continue; fi
  rm -rf "$TOOLS/$name"; mkdir -p "$TOOLS/$name"
  tar xzf "$tgz" -C "$TOOLS/$name"
  chmod -R a+rX "$TOOLS/$name"        # vendor tarballs unpack with 0700 folders
  echo "installed $name  ($top)"; n=$((n+1))
done
[ "$n" -gt 0 ] || echo "no tool tarballs installed from $SRC" >&2
chmod a+rx "$TOOLS"

lics=("$SRC"/*.lic)
if [ "${#lics[@]}" -gt 0 ]; then
  mkdir -p "$LIC"; chmod 0755 "$LIC"
  for f in "${lics[@]}"; do install -m 0644 "$f" "$LIC/$(basename "$f")"; done
  echo "licence copied to $LIC (contents not displayed)"
else
  echo "no .lic found in $SRC" >&2
fi

# AutoDock Vina (Apache-2.0): downloaded from the project's release page, checksum-pinned.
VINA_SHA256="f31f774f723bba7bbe6e9d1c47577020eea9a8da16424284c043d22593570644"
if [ ! -x "$TOOLS/vina/vina" ]; then
  mkdir -p "$TOOLS/vina"
  curl -fsSL -o "$TOOLS/vina/vina.tmp" "https://github.com/ccsb-scripps/AutoDock-Vina/releases/download/v1.2.7/vina_1.2.7_linux_x86_64"
  echo "$VINA_SHA256  $TOOLS/vina/vina.tmp" | sha256sum -c - >/dev/null || { rm -f "$TOOLS/vina/vina.tmp"; echo "!! Vina checksum mismatch" >&2; exit 1; }
  mv "$TOOLS/vina/vina.tmp" "$TOOLS/vina/vina"; chmod 0755 "$TOOLS/vina/vina"; chmod -R a+rX "$TOOLS/vina"
  echo "installed vina (v1.2.7, sha256 verified)"
fi

echo; echo "== vendor diagnostics"
export BIOSOLVE_LICENSE_FILE="$LIC"
echo "vina: $("$TOOLS/vina/vina" --version 2>&1 | sed -n 1p)"
for t in flexx hydescorer fastgrow; do
  exe="$(find "$TOOLS/$t" -maxdepth 3 \( -type f -o -type l \) -name "$t" 2>/dev/null | sed -n 1p)"
  if [ -z "$exe" ]; then echo "$t: executable not found"; continue; fi
  v="$("$exe" --version 2>&1 | grep -E '^version' | sed -n 1p || true)"
  l="$("$exe" --license-info 2>&1 | grep -E '>>' | sed -n 1p || true)"
  echo "$t: ${v:-version ?} | ${l:-no licence line (check output of '$exe --license-info')}"
done
