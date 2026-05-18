"""Entry point for `python -m plsearch.cli "query"`."""

import sys

from plsearch.cli.client import main

if __name__ == "__main__":
    sys.exit(main())
