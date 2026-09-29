#!/usr/bin/env bash
# Upload the day files listed in out/written.txt to monthly releases (data-YYYY-MM), then the
# manifest. The manifest goes last, so a failed upload never marks a day as published.
set -euo pipefail
[ -f out/written.txt ] || { echo "Nothing to publish"; exit 0; }
sort -u out/written.txt | while read -r f; do
  [ -f "$f" ] || continue
  ym=$(basename "$f" | sed -E 's/^.*_([0-9]{4})-([0-9]{2})-[0-9]{2}\.parquet$/\1-\2/')
  tag="data-$ym"
  gh release view "$tag" >/dev/null 2>&1 \
    || gh release create "$tag" --title "Inventory $ym" --latest=false \
         --notes "Daily Sentinel-1 RFI annotation inventory files for $ym. See the README for the schema."
  gh release upload "$tag" "$f" --clobber
done
if [ -f state/manifest.csv ]; then
  gh release view manifest >/dev/null 2>&1 \
    || gh release create manifest --title "Manifest" --latest=false --notes "Which days are done (state for the harvester)."
  gh release upload manifest state/manifest.csv --clobber
fi
