import pathlib
import sys


producer_input = pathlib.Path(sys.argv[1])
template = pathlib.Path(sys.argv[2])
output = pathlib.Path(sys.argv[3])

producer_input.read_bytes()
output.write_bytes(template.read_bytes())
