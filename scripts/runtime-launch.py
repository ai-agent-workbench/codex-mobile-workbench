#!/usr/bin/env python3
"""Installed controller entry: never resolves application via a mutable alias."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared.runtime_controller import main
raise SystemExit(main())
