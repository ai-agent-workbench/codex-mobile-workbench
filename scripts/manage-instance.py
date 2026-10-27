#!/usr/bin/env python3
"""Local Windows-panel entry. Never install a web control plane or shell RPC."""
from pathlib import Path
import sys

# Inspection must not add caches to a sealed source package.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.instance_manager import main

raise SystemExit(main())
