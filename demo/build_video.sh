#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
shots="$root/docs/images"
out="$root/docs/media/tlf-review-demo.mp4"
list_file="$(mktemp "${TMPDIR:-/tmp}/tlf-demo-slides.XXXXXX")"
trap 'rm -f "$list_file"' EXIT

for needed in 08_architecture.png 01_dashboard.png 06_toc.png 02_table_mismatch.png \
              03_cross_output.png 04_ai_review.png 05_comments.png 07_benchmark_report.png; do
  test -f "$shots/$needed" || { echo "Missing screenshot: $shots/$needed" >&2; exit 1; }
done

printf "file '%s'\nduration 12\n" "$shots/08_architecture.png" >> "$list_file"
printf "file '%s'\nduration 13\n" "$shots/01_dashboard.png" >> "$list_file"
printf "file '%s'\nduration 15\n" "$shots/06_toc.png" >> "$list_file"
printf "file '%s'\nduration 20\n" "$shots/02_table_mismatch.png" >> "$list_file"
printf "file '%s'\nduration 18\n" "$shots/03_cross_output.png" >> "$list_file"
printf "file '%s'\nduration 18\n" "$shots/04_ai_review.png" >> "$list_file"
printf "file '%s'\nduration 12\n" "$shots/05_comments.png" >> "$list_file"
printf "file '%s'\nduration 12\nfile '%s'\n" "$shots/07_benchmark_report.png" "$shots/07_benchmark_report.png" >> "$list_file"

cd "$root"
ffmpeg -hide_banner -loglevel warning -y \
  -f concat -safe 0 -i "$list_file" \
  -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=white,subtitles=demo/video_captions.srt:force_style='FontName=Arial,FontSize=10,PrimaryColour=&H00FFFFFF,BackColour=&HA0000000,BorderStyle=4,Outline=0,Shadow=0,MarginV=18,Alignment=2'" \
  -t 120 -r 30 -c:v libx264 -preset medium -crf 21 -pix_fmt yuv420p \
  -movflags +faststart \
  -metadata title="TLF Review Platform — synthetic two-minute demonstration" \
  -metadata comment="All data and model behavior shown are simulated; not clinically validated." \
  "$out"

echo "$out"
shasum -a 256 "$out"
