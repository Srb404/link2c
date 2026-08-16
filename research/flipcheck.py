#!/usr/bin/env python3
"""
Decide, per swept selector, whether the camera flipped its output.

Each phase's last frame is compared against the baseline frame and against
the baseline mirrored horizontally, mirrored vertically, and rotated 180.
Whichever comparison is closest names the transform; the scene is a static
wall, so a genuine flip stands out far beyond frame-to-frame noise.
"""
import glob
import sys

import numpy as np
from PIL import Image

OUT = sys.argv[1] if len(sys.argv) > 1 else "fl2"
PHASES = sys.argv[2] if len(sys.argv) > 2 else "fl2phases.txt"


def load(path):
    return np.asarray(Image.open(path).convert("L").resize((320, 180)), np.float32)


frames = sorted(glob.glob(f"{OUT}/x_*.jpg"))
if not frames:
    sys.exit(f"no frames in {OUT}/")

phases = []
for line in open(PHASES):
    parts = line.split()
    if len(parts) == 4 and parts[0].isdigit():
        phases.append((int(parts[0]), parts[1], parts[2], parts[3]))
if not phases:
    sys.exit(f"no phases in {PHASES}")

base = load(frames[min(phases[0][0], len(frames)) - 1])
variants = {
    "unchanged": base,
    "MIRROR H": base[:, ::-1],
    "MIRROR V": base[::-1, :],
    "ROTATE 180": base[::-1, ::-1],
}

# How much two unrelated-but-untouched frames differ: the noise floor.
noise = np.abs(load(frames[0]) - base).mean()
print(f"reference frame: {frames[min(phases[0][0], len(frames)) - 1]}")
print(f"noise floor: {noise:.2f}\n")
print(f"{'selector':<14} {'value':<6} {'verdict':<16} " +
      "  ".join(f"{k:>14}" for k in variants))
print("-" * 96)

hits = []
for i, (start, unit, sel, val) in enumerate(phases):
    if unit == "BASE":
        continue
    end = phases[i + 1][0] - 1 if i + 1 < len(phases) else len(frames)
    idx = min(end, len(frames)) - 1
    if idx < 0:
        continue
    cur = load(frames[idx])

    scores = {k: np.abs(cur - v).mean() for k, v in variants.items()}
    best = min(scores, key=scores.get)
    second = sorted(scores.values())[1]
    # Only believe a flip when it beats the runner-up clearly.
    verdict = best if best != "unchanged" and scores[best] * 1.5 < second else "-"
    if verdict != "-":
        hits.append((unit, sel, val, verdict))

    print(f"unit {unit:>2} sel {sel:>2}   0x{val:<4} {verdict:<16} " +
          "  ".join(f"{scores[k]:14.2f}" for k in variants))

print()
if hits:
    print("FOUND:")
    for unit, sel, val, what in hits:
        print(f"  unit {unit} selector {sel} = 0x{val}  ->  {what}")
else:
    print("no tested selector flipped the image")
