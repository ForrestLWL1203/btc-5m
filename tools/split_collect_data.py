"""Split a multi-window collect_*.jsonl file into one file per window.

The collector can append several market windows into one JSONL file. Many
analysis scripts treat each input file as a single window, so this helper
breaks the file at each `src="outcome"` marker.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def split_collect_file(path: Path, output_dir: Path | None = None) -> list[Path]:
    output_dir = output_dir or path.parent / f"{path.stem}_split"
    output_dir.mkdir(parents=True, exist_ok=True)

    chunks: list[list[str]] = []
    current: list[str] = []

    with path.open() as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            current.append(line)
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("src") == "outcome":
                chunks.append(current)
                current = []

    if current:
        chunks.append(current)

    written: list[Path] = []
    for idx, chunk in enumerate(chunks, start=1):
        out_path = output_dir / f"{path.stem}.window{idx:02d}.jsonl"
        out_path.write_text("\n".join(chunk) + "\n")
        written.append(out_path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Split a collect_*.jsonl file into per-window files.")
    parser.add_argument("file", help="Input JSONL file")
    parser.add_argument("--output-dir", help="Directory for split files")
    args = parser.parse_args()

    paths = split_collect_file(
        Path(args.file),
        Path(args.output_dir) if args.output_dir else None,
    )
    print(f"Wrote {len(paths)} files:")
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
