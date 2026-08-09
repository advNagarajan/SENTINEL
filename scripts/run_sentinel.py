"""CLI entrypoint script for running Project SENTINEL targets."""
import os
import sys

# Ensure repository root is on Python module search path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main

if __name__ == "__main__":
    main.cli()
