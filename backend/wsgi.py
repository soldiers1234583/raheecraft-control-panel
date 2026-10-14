#!/usr/bin/env python3
"""
WSGI entry point for Render deployment.
Render uses this to start the application.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.app import app

# This is the WSGI application that Render will use
application = app

if __name__ == "__main__":
    # For local development
    from backend.config import config
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
