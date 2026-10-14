#!/usr/bin/env python3
"""
Run RaheeCraft backend tests.
"""

import unittest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Discover and run tests
loader = unittest.TestLoader()
suite = loader.discover('backend/tests', pattern='test_*.py')

runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)

# Exit with appropriate code
sys.exit(0 if result.wasSuccessful() else 1)
