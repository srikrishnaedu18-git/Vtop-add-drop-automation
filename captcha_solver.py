"""
captcha_solver.py
-----------------
Python port of ViBoot's neural-network CAPTCHA solver (captchaparser.js → solve()).
Works on VIT A&D portal and main VTOP: 6 uppercase alphanumeric chars,
image 200×40 px, saturation-based segmentation + single-layer NN.

Usage:
    from captcha_solver import solve_captcha_b64
    text = solve_captcha_b64(base64_data_uri)  # → e.g. "WRTF4D"
"""

import base64
import io
import math
import numpy as np
from PIL import Image
from bitmaps import NN_WEIGHTS, NN_BIASES

# Characters the NN was trained on (must match ViBoot's label_txt)
LABEL_TXT = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

HEIGHT = 40
WIDTH = 200


# ─── Pre-processing (port of saturation() in captchaparser.js) ────────────────

def _saturation(pixels: np.ndarray) -> list:
    """
    Convert RGBA/RGB pixel array to per-pixel saturation value,
    then reshape into 6 character block sub-arrays.
    pixels: numpy array shape (H, W, C) — already RGB
    Returns: list of 6 sub-images, each shape ~(28, 23)
    """
    h, w = pixels.shape[:2]
    flat = pixels.reshape(-1, pixels.shape[2]).astype(np.float32)

    sat = np.zeros(flat.shape[0], dtype=np.float32)
    mins = flat.min(axis=1)
    maxs = flat.max(axis=1)
    nonzero = maxs != 0
    sat[nonzero] = np.round((maxs[nonzero] - mins[nonzero]) * 255 / maxs[nonzero])

    img2d = sat.reshape(HEIGHT, WIDTH)

    blocks = []
    for i in range(6):
        x1 = (i + 1) * 25 + 2
        y1 = 7 + 5 * (i % 2) + 1
        x2 = (i + 2) * 25 + 1
        y2 = 35 - 5 * ((i + 1) % 2)
        block = img2d[y1:y2, x1:x2]
        blocks.append(block)

    return blocks


def _pre_img(block: np.ndarray) -> np.ndarray:
    """Threshold block to binary 0/1 using pixel mean."""
    avg = block.mean()
    return (block > avg).astype(np.float32)


def _flatten(arr: np.ndarray) -> np.ndarray:
    return arr.flatten()


# ─── Neural network forward pass ─────────────────────────────────────────────

def _softmax(a: np.ndarray) -> np.ndarray:
    e = np.exp(a - a.max())  # subtract max for numerical stability
    return e / e.sum()


_weights = np.array(NN_WEIGHTS, dtype=np.float32)   # shape: (input_size, num_classes)
_biases  = np.array(NN_BIASES,  dtype=np.float32)   # shape: (num_classes,)


def _predict_char(block: np.ndarray) -> str:
    """Run the NN on a single character block, return predicted char."""
    binary = _pre_img(block)
    flat   = _flatten(binary).reshape(1, -1)           # (1, input_size)
    logits = flat @ _weights + _biases                 # (1, num_classes)
    probs  = _softmax(logits[0])
    idx    = int(np.argmax(probs))
    return LABEL_TXT[idx]


# ─── Public API ───────────────────────────────────────────────────────────────

def solve_captcha_b64(b64_string: str) -> str:
    """
    Given the data:image/jpeg;base64,... string from the CAPTCHA <img>,
    returns the 6-character decoded captcha text (e.g. 'WRTF4D').
    """
    if b64_string.startswith("data:"):
        b64_string = b64_string.split(",", 1)[1]

    img_bytes = base64.b64decode(b64_string)
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

    # Ensure the image is 200×40 (resize if needed)
    if img.size != (WIDTH, HEIGHT):
        img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)

    pixels = np.array(img, dtype=np.float32)
    blocks = _saturation(pixels)

    result = ""
    for block in blocks:
        result += _predict_char(block)

    return result


def solve_captcha_file(path: str) -> str:
    """Helper: solve from a local image file path."""
    with open(path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode()
    return solve_captcha_b64(b64)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: venv/bin/python captcha_solver.py <image_path>")
        sys.exit(1)
    arg = sys.argv[1]
    if arg.startswith("data:") or (len(arg) > 200 and "/" not in arg):
        result = solve_captcha_b64(arg)
    else:
        result = solve_captcha_file(arg)
    print(f"Solved CAPTCHA: {result}")
