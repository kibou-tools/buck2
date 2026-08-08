import pathlib
import sys


pathlib.Path(sys.argv[1]).read_bytes()
pathlib.Path(sys.argv[3]).write_bytes(pathlib.Path(sys.argv[2]).read_bytes())
