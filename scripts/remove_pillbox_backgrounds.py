#!/usr/bin/env python3
"""Strip the photo background from each NLM Pillbox image.

Walks data/pillbox/images/*.jpg, runs each through rembg (u2netp by default —
small, fast, decent quality for pills on uniform backgrounds), and writes the
result as a PNG with alpha to data/pillbox/images_nobg/<name>.png.

Idempotent and resumable: any output that already exists is skipped, so you
can kill the process and re-run.

Setup (one-time):
    python3 -m venv data/pillbox/.venv
    source data/pillbox/.venv/bin/activate
    pip install --upgrade pip
    pip install rembg onnxruntime pillow

Run:
    python3 scripts/remove_pillbox_backgrounds.py [--model u2netp|isnet-general-use]
                                                  [--limit N] [--workers N]
                                                  [--src DIR] [--dst DIR]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


# ── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_REPO = Path(__file__).resolve().parent.parent
DEFAULT_SRC = DEFAULT_REPO / "data" / "pillbox" / "images"
DEFAULT_DST = DEFAULT_REPO / "data" / "pillbox" / "images_nobg"
DEFAULT_MODEL = "u2netp"   # small + fast; switch to isnet-general-use for better quality


def iter_inputs(src: Path):
    """Yield .jpg/.jpeg/.png paths under src, sorted for stable progress output."""
    exts = {".jpg", ".jpeg", ".png"}
    return sorted(p for p in src.iterdir() if p.is_file() and p.suffix.lower() in exts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC,
                    help=f"Source dir (default: {DEFAULT_SRC})")
    ap.add_argument("--dst", type=Path, default=DEFAULT_DST,
                    help=f"Destination dir (default: {DEFAULT_DST})")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"rembg model name (default: {DEFAULT_MODEL})")
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after N images (0 = no limit). Useful for sample QA.")
    ap.add_argument("--progress-every", type=int, default=50,
                    help="Log progress every N images (default: 50)")
    args = ap.parse_args()

    # Import rembg lazily so --help works even before the venv is set up.
    try:
        from rembg import new_session, remove
        from PIL import Image
    except ImportError as e:
        print(
            "ERROR: rembg/Pillow not installed.\n"
            "  python3 -m venv data/pillbox/.venv\n"
            "  source data/pillbox/.venv/bin/activate\n"
            "  pip install rembg onnxruntime pillow\n"
            f"  ({e})",
            file=sys.stderr,
        )
        return 1

    if not args.src.is_dir():
        print(f"ERROR: source directory does not exist: {args.src}", file=sys.stderr)
        return 1

    args.dst.mkdir(parents=True, exist_ok=True)

    inputs = iter_inputs(args.src)
    if args.limit:
        inputs = inputs[: args.limit]
    total = len(inputs)
    if not total:
        print(f"No images found in {args.src}")
        return 0

    print(f"Source:      {args.src}")
    print(f"Destination: {args.dst}")
    print(f"Model:       {args.model}")
    print(f"Inputs:      {total} files")
    print(f"Loading model session…")
    session = new_session(args.model)
    print(f"Model ready. Starting.\n")

    started = time.time()
    skipped = 0
    processed = 0
    failed = 0

    for i, src_path in enumerate(inputs, start=1):
        dst_path = args.dst / (src_path.stem + ".png")
        if dst_path.exists():
            skipped += 1
            continue

        try:
            with Image.open(src_path) as img:
                # rembg.remove accepts a PIL Image and returns a PIL Image with alpha.
                cutout = remove(img, session=session)
                cutout.save(dst_path, format="PNG", optimize=True)
            processed += 1
        except Exception as e:
            failed += 1
            print(f"  ! {src_path.name}: {e}", file=sys.stderr)

        if i % args.progress_every == 0 or i == total:
            elapsed = time.time() - started
            done = processed + skipped + failed
            rate = (processed / elapsed) if elapsed > 0 and processed else 0
            remaining = total - done
            eta = (remaining / rate) if rate > 0 else float("inf")
            eta_str = f"{int(eta // 60)}m{int(eta % 60):02d}s" if eta != float("inf") else "—"
            print(
                f"  [{i:5d}/{total}]  processed={processed}  skipped={skipped}  "
                f"failed={failed}  rate={rate:.2f}/s  ETA={eta_str}",
                flush=True,
            )

    elapsed = time.time() - started
    print(
        f"\nDone in {int(elapsed // 60)}m{int(elapsed % 60):02d}s. "
        f"processed={processed}  skipped={skipped}  failed={failed}"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
