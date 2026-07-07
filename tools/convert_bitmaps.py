"""
convert_bitmaps.py
------------------
Converts ViBoot's bitmaps.js into bitmaps.py (Python dict).
Handles both character bitmap arrays AND neural network weights/biases.
Run once: venv/bin/python3 convert_bitmaps.py
"""
import re, json, pathlib

BASE_DIR = pathlib.Path(__file__).parent.parent.absolute()
src_file = BASE_DIR / "reference" / "bitmaps.js"
out_file = BASE_DIR / "src" / "bitmaps.py"

src = src_file.read_text()

src = src.strip()
src = re.sub(r'^const bitmaps\s*=\s*', '', src)
src = re.sub(r';\s*$', '', src)

# Quote bare word/number keys (JS → JSON)
src = re.sub(r'(\n\s*)([A-Z0-9]+)(\s*:)', r'\1"\2"\3', src)
# Remove trailing commas before } or ]
src = re.sub(r',(\s*[\}\]])', r'\1', src)

try:
    data = json.loads(src)
    char_keys = [k for k in data.keys() if k not in ("weights", "biases")]
    print(f"Parsed {len(char_keys)} character entries + weights + biases.")
except json.JSONDecodeError as e:
    print(f"JSON parse error: {e}")
    lines = src.splitlines()
    lineno = e.lineno
    for i in range(max(0, lineno-3), min(len(lines), lineno+3)):
        print(f"{i+1:4d}: {lines[i]}")
    raise

out_lines = ["# Auto-generated from ViBoot's bitmaps.js — DO NOT EDIT\n\n"]

# Write character bitmaps
out_lines.append("BITMAPS = {\n")
for key, val in data.items():
    if key not in ("weights", "biases"):
        out_lines.append(f"    {repr(key)}: {repr(val)},\n")
out_lines.append("}\n\n")

# Write NN weights and biases separately for easy import
out_lines.append(f"NN_WEIGHTS = {repr(data['weights'])}\n\n")
out_lines.append(f"NN_BIASES = {repr(data['biases'])}\n")

out_file.write_text("".join(out_lines))
print("Written to src/bitmaps.py ✓")

