#!/usr/bin/env python3
"""
Kaggle Bedrock Server Tunnel Helper
===================================
For Bedrock Edition, Kaggle doesn't have native server binaries.
This script helps you tunnel a locally-running Bedrock server to the internet.

Usage:
    1. Run a Bedrock server on your local machine
    2. Run this script to start an ngrok tunnel
    3. Share the ngrok address with friends
"""

import subprocess
import time
import sys

# Configuration
LOCAL_PORT = 19132  # Default Bedrock port
TUNNEL_TYPE = "tcp"  # Bedrock needs UDP, but ngrok TCP fallback works

# ==== SETUP ====

def check_requirements():
    """Check if required tools are available"""
    print("🔍 Checking requirements...")
    
    # Check ngrok
    try:
        result = subprocess.run(['ngrok', '--version'], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ ngrok found: {result.stdout.split()[1]}")
        else:
            print("❌ ngrok not found. Install from https://ngrok.com")
            return False
    except FileNotFoundError:
        print("❌ ngrok not found. Install from https://ngrok.com")
        return False
    
    # Check Python pyngrok
    try:
        import pyngrok
        print("✅ pyngrok installed")
    except ImportError:
        print("⚠️  pyngrok not installed. Installing...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyngrok"])
        print("✅ pyngrok installed")
    
    return True

def start_bedrock_server():
    """Start the Bedrock server (run this on your local machine)"""
    print("\n📦 Starting Bedrock Dedicated Server...")
    print("   Make sure bedrock_server binary is in current directory")
    
    # This assumes you have the Bedrock server binary
    # On Kaggle, you would need to download it separately
    try:
        proc = subprocess.Popen(
            ["./bedrock_server"],
            cwd="/path/to/bedrock/server",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        print(f"✅ Bedrock server started (PID: {proc.pid})")
        return proc
    except Exception as e:
        print(f"❌ Failed to start: {e}")
        return None

def setup_ngrok(auth_token=None):
    """Setup and start ngrok tunnel"""
    print("\n🔧 Setting up ngrok tunnel...")
    
    from pyngrok import ngrok
    
    if auth_token:
        ngrok.set_auth_token(auth_token)
        print("✅ Auth token configured")
    
    print(f"   Tunneling port {LOCAL_PORT} ({TUNNEL_TYPE})...")
    
    try:
        tunnel = ngrok.connect(LOCAL_PORT, TUNNEL_TYPE)
        print(f"\n✅ Tunnel established!")
        print(f"📍 Public address: {tunnel.public_url}")
        print(f"📍 Connection info:")
        print(f"   Address: {tunnel.public_url.replace('tcp://', '').replace('udp://', '')}")
        print(f"   Port: {LOCAL_PORT}")
        return tunnel
    except Exception as e:
        print(f"❌ Tunnel failed: {e}")
        print("\n💡 Make sure your ngrok auth token is correct")
        print("   Get one at: https://dashboard.ngrok.com/get-started/your-authtoken")
        return None

def monitor_tunnel(tunnel):
    """Monitor the tunnel and server"""
    print("\n📊 Monitoring...")
    print("   Press Ctrl+C to stop")
    
    try:
        while True:
            time.sleep(10)
            # Check if tunnel is still active
            # In practice, just let it run
    except KeyboardInterrupt:
        print("\n🛑 Stopping...")
        tunnel.close()
        print("✅ Tunnel closed")

def main():
    print("=" * 60)
    print("KAGGLE BEDROCK SERVER TUNNEL")
    print("=" * 60)
    
    # Configuration
    AUTH_TOKEN = ""  # ← Paste your ngrok token here
    
    if not check_requirements():
        print("\n❌ Setup failed - check requirements")
        return
    
    # For Bedrock on Kaggle, you need:
    # 1. The Bedrock server binary (Linux ARM64 or x86_64)
    # 2. A tunnel to expose it
    
    # Since Kaggle doesn't support Bedrock natively, 
    # this is a helper for when you run Bedrock locally
    print("\n📌 NOTE: Kaggle does NOT support Bedrock natively.")
    print("   This script is for tunneling a LOCAL Bedrock server.")
    print("\n   For Kaggle, use Java Edition instead (see setup notebook)")
    
    # If you have a local Bedrock server running, tunnel it
    print("\n--- Tunnel Options ---")
    print("1. Tunnel local Bedrock server")
    print("2. Just show ngrok setup instructions")
    
    choice = input("Choose (1 or 2): ").strip()
    
    if choice == "1":
        # Start local server (your machine, not Kaggle)
        server_proc = start_bedrock_server()
        
        if server_proc:
            # Setup tunnel
            tunnel = setup_ngrok(AUTH_TOKEN)
            
            if tunnel:
                print("\n✅ Ready! Share this address with friends:")
                print(f"   {tunnel.public_url}")
                monitor_tunnel(tunnel)
    else:
        print("\n📋 ngrok Setup Instructions:")
        print("-" * 40)
        print("1. Install ngrok: https://ngrok.com/download")
        print("2. Get auth token: https://dashboard.ngrok.com")
        print("3. Connect: ngrok config add-authtoken YOUR_TOKEN")
        print(f"4. Start tunnel: ngrok tcp {LOCAL_PORT}")
        print("-" * 40)

if __name__ == "__main__":
    main()
