import pathlib
import sys


content = pathlib.Path(sys.argv[1]).read_text().strip()
print(f"resource={content}")
raise SystemExit(1 if content == "fail" else 0)
