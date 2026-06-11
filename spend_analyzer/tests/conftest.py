import sys
from pathlib import Path

# make the repo root importable when running `pytest` from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
