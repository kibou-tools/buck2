import pathlib
import sys


resource = pathlib.Path(sys.argv[1])

if "--list" in sys.argv:
    print("test1")
    raise SystemExit(0)

content = resource.read_text().strip()
print(f"resource={content}")
raise SystemExit(1 if content == "fail" else 0)
