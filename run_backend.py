#!/usr/bin/env python3
"""
Run the RaheeCraft Control Panel backend.
"""

import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.app import main

if __name__ == "__main__":
    main()
