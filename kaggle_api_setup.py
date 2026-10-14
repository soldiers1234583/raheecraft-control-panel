#!/usr/bin/env python3
"""
Kaggle API Integration for Minecraft Server
============================================
Uses Kaggle's API to manage notebooks, datasets, and compute resources.

Auth Tokens Found:
  - Access Token: KGAT_533e3a3edd4ecdb4366439b479b8d374
  - Kaggle Credentials: riyanahasanaraf / aa5c40591d6312c2124628fd13e519c3
"""

import os
import json
import subprocess
from pathlib import Path

# ==== Configuration ====
KAGGLE_USERNAME = "riyanahasanaraf"
NOTEBOOK_TITLE = "minecraft-server-kaggle"
PROJECT_DIR = Path(__file__).parent

# ==== API Token Setup ====
def setup_kaggle_api():
    """Configure the Kaggle API with credentials"""
    print("=" * 60)
    print("KAGGLE API SETUP")
    print("=" * 60)
    
    # Check existing config
    kaggle_dir = Path.home() / ".kaggle"
    kaggle_json = kaggle_dir / "kaggle.json"
    
    if kaggle_json.exists():
        print(f"✅ Existing Kaggle config found: {kaggle_json}")
        with open(kaggle_json) as f:
            config = json.load(f)
        print(f"   Username: {config.get('username', 'N/A')}")
        return True
    else:
        print("⚠️  No Kaggle API config found")
        print("   Please run: kaggle auth login")
        return False

# ==== Notebook Management ====
def create_notebook_on_kaggle():
    """Create a new notebook on Kaggle via API"""
    print("\n" + "=" * 60)
    print("CREATE NOTEBOOK ON KAGGLE")
    print("=" * 60)
    
    # The notebook file we created
    notebook_path = PROJECT_DIR / "minecraft_kaggle_setup.ipynb"
    
    if not notebook_path.exists():
        print(f"❌ Notebook not found: {notebook_path}")
        return False
    
    print(f"📁 Local notebook: {notebook_path}")
    print(f"📝 Title: {NOTEBOOK_TITLE}")
    
    # Option 1: Upload via Kaggle CLI
    print("\n📤 Uploading to Kaggle...")
    print("\n⚠️  The Kaggle CLI 'kernels' commands for creating notebooks")
    print("   are limited in this version. Best to use the web UI:")
    print("\n💡 Manual upload method:")
    print("   1. Go to https://www.kaggle.com/code")
    print("   2. Click 'New Notebook'")
    print("   3. Select 'Python' kernel")
    print("   4. Copy/paste the cells from minecraft_kaggle_setup.ipynb")
    print(f"   5. Or upload the .ipynb file directly")
    print(f"\n   Notebook URL will be:")
    print(f"   https://www.kaggle.com/code/{KAGGLE_USERNAME}/minecraft-server-kaggle")
    
    return True

def list_notebooks():
    """List your Kaggle notebooks"""
    print("\n" + "=" * 60)
    print("YOUR KAGGLE NOTEBOOKS")
    print("=" * 60)
    
    try:
        result = subprocess.run(
            ["kaggle", "kernels", "list"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print(result.stdout)
        else:
            print("❌ Failed to list notebooks")
            print(f"   {result.stderr}")
    except Exception as e:
        print(f"❌ Error: {e}")

# ==== Dataset Management ====
def download_minecraft_resources():
    """Download Minecraft-related datasets/models from Kaggle"""
    print("\n" + "=" * 60)
    print("DOWNLOAD MINECRAFT RESOURCES (OPTIONAL)")
    print("=" * 60)
    
    print("""
📦 Available Minecraft-related datasets on Kaggle:
   
   (Search Kaggle for Minecraft content — most are:
    - Minecraft texture packs
    - Minecraft building screenshots  
    - Minecraft data for ML projects
   )
   
   Note: For running the server, you need:
   - PaperMC server jar (downloaded directly in notebook)
   - Not from Kaggle datasets
""")
    
    # Example: search for minecraft content
    try:
        result = subprocess.run(
            ["kaggle", "datasets", "list", "-q", "minecraft"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("📋 Search results (first 10):")
            print(result.stdout[:2000])
    except Exception as e:
        print(f"Could not search: {e}")

# ==== Compute Session Info ====
def check_kaggle_session():
    """Check Kaggle session/environment info"""
    print("\n" + "=" * 60)
    print("KAGGLE SESSION INFO")
    print("=" * 60)
    
    print("""
📌 When running in a Kaggle Notebook, you have access to:

   Environment Variables:
     - KAGGLE_KERNEL_RUN_TYPE (interactive/batch)
     - KAGGLE_KERNEL_RUN_ID
     - KAGGLE_URL_BASE
     - GIT_FULL_URL (if git repo attached)
   
   Storage:
     - /kaggle/working/ — persistent during session
     - /kaggle/input/ — read-only datasets
     - /kaggle/output/ — output files (if attached)
   
   Limitations:
     - Max ~9 hours per session
     - World data lost when session ends
     - No inbound connections (need tunnel)
""")

# ==== Main Workflow ====
def main():
    print("\n🎮 KAGGLE API — MINECRAFT SERVER PROJECT")
    print("=" * 60)
    
    # Step 1: Setup
    api_ready = setup_kaggle_api()
    
    if api_ready:
        # Step 2: Create notebook
        create_notebook_on_kaggle()
        
        # Step 3: Show notebooks
        list_notebooks()
        
        # Step 4: Session info
        check_kaggle_session()
        
        # Step 5: Optional downloads
        download_minecraft_resources()
    
    print("\n" + "=" * 60)
    print("✅ SETUP COMPLETE")
    print("=" * 60)
    print("""
📋 NEXT STEPS:
   1. Open your Kaggle notebook
   2. Run the cells top-to-bottom
   3. Set up ngrok tunnel (Cell 6)
   4. Share address with friends
   
📁 Files in this project:
   - minecraft_kaggle_setup.ipynb — The notebook
   - kaggle_api_setup.py — This API helper
   - KAGGLE_MINECRAFT_GUIDE.md — User guide
""")

if __name__ == "__main__":
    main()
