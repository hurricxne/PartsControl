"""python -m portal.tests.mutaciones [parte-del-nombre-de-la-suite ...]"""
import sys

from . import main

raise SystemExit(main(sys.argv[1:]))
