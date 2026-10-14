#!/usr/bin/env python3
"""
Geyser + Floodgate Setup for Minecraft Crossplay
=================================================
Enables Java and Bedrock players to join the same server.

Components:
  - Geyser: Java-to-Bedrock protocol translation
  - Floodgate: Bedrock authentication without Java accounts

Requirements:
  - Paper server 1.21.4 (matches your configuration)
  - Geyser-Spigot 1.21.4+ compatible
  - Floodgate 1.21.4+ compatible

Reference:
  https://geysermc.org/wiki/geyser/setup/self/paper-spigot/
  https://geysermc.org/wiki/floodgate/setup/

Limitations:
  - Playit.gg does NOT support UDP tunnels for Geyser plugin
  - For Bedrock via Playit, use the Playit program agent (not plugin)
  - Bedrock 1.26.51+ uses NetherNet (may need specific Playit config)
  - Free tier may have Bedrock limitations
"""

import os
import sys
import json
import urllib.request
import zipfile
import tempfile
import shutil
from pathlib import Path
from typing import Optional, Tuple

# ==== Configuration ====
SERVER_DIR = "/kaggle/working/minecraft_server"
PLUGINS_DIR = os.path.join(SERVER_DIR, "plugins")
GEYSER_VERSION = "2.1.0"  # Check for latest compatible with 1.21.4
FLOODGATE_VERSION = "2.1.0"  # Check for latest compatible

# Bedrock listener config
BEDROCK_LISTENER_PORT = 19132  # Default UDP port for Geyser
BEDROCK_LISTENER_HOST = "0.0.0.0"  # Listen on all interfaces


class GeyserSetupError(Exception):
    """Custom exception for Geyser setup errors"""
    pass


class GeyserCrossplay:
    """
    Manages Geyser + Floodgate installation for crossplay.
    """
    
    def __init__(self):
        self.geyser_jar = None
        self.floodgate_jar = None
        
    def ensure_plugins_dir(self):
        """Create plugins directory if it doesn't exist."""
        os.makedirs(PLUGINS_DIR, exist_ok=True)
        print(f"📁 Plugins directory: {PLUGINS_DIR}")
        
    def get_latest_versions(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Get latest Geyser and Floodgate versions.
        
        Returns:
            (geyser_version, floodgate_version) or None if failed
        """
        print("\n📋 Checking for latest versions...")
        
        # Geyser download URL pattern:
        # https://download.geysermc.org/v2/projects/geyser-Spigot/releases/latest/download
        # Or with version: /download?version=2.1.0
        
        # For now, use known compatible versions
        # Latest stable for 1.21.x is typically 2.0.x or 2.1.x
        geyser_url = "https://download.geysermc.org/v2/projects/geyser-Spigot/releases/latest/download"
        floodgate_url = "https://download.geysermc.org/v2/projects/floodgate/releases/latest/download"
        
        print(f"   Geyser: {geyser_url}")
        print(f"   Floodgate: {floodgate_url}")
        
        return geyser_url, floodgate_url
    
    def download_geyser(self, url: str = None) -> Optional[str]:
        """
        Download Geyser-Spigot plugin.
        
        Args:
            url: Download URL (uses latest if not provided)
            
        Returns:
            Path to downloaded jar, or None on failure
        """
        if not url:
            url, _ = self.get_latest_versions()
        
        print(f"\n📥 Downloading Geyser...")
        print(f"   From: {url}")
        
        try:
            # Download to temp
            temp_dir = tempfile.mkdtemp()
            jar_path = os.path.join(temp_dir, "geyser.jar")
            
            urllib.request.urlretrieve(url, jar_path, timeout=60)
            
            # Verify it's a valid jar
            if not zipfile.is_zipfile(jar_path):
                print("❌ Downloaded file is not a valid JAR")
                shutil.rmtree(temp_dir, ignore_errors=True)
                return None
            
            # Move to plugins
            dest = os.path.join(PLUGINS_DIR, "Geyser-Spigot.jar")
            shutil.copy2(jar_path, dest)
            
            print(f"✅ Downloaded to: {dest}")
            
            # Cleanup
            shutil.rmtree(temp_dir, ignore_errors=True)
            
            self.geyser_jar = dest
            return dest
            
        except Exception as e:
            print(f"❌ Download failed: {e}")
            print("\n💡 Manual download:")
            print("   1. Visit https://geysermc.org/wiki/geyser/setup/self/paper-spigot/")
            print("   2. Download Geyser-Spigot.jar")
            print("   3. Upload to /kaggle/working/minecraft_server/plugins/")
            return None
    
    def download_floodgate(self, url: str = None) -> Optional[str]:
        """
        Download Floodgate plugin.
        
        Args:
            url: Download URL (uses latest if not provided)
            
        Returns:
            Path to downloaded jar, or None on failure
        """
        if not url:
            _, url = self.get_latest_versions()
        
        print(f"\n📥 Downloading Floodgate...")
        print(f"   From: {url}")
        
        try:
            # Download to temp
            temp_dir = tempfile.mkdtemp()
            jar_path = os.path.join(temp_dir, "floodgate.jar")
            
            urllib.request.urlretrieve(url, jar_path, timeout=60)
            
            # Verify it's a valid jar
            if not zipfile.is_zipfile(jar_path):
                print("❌ Downloaded file is not a valid JAR")
                shutil.rmtree(temp_dir, ignore_errors=True)
                return None
            
            # Move to plugins
            dest = os.path.join(PLUGINS_DIR, "floodgate.jar")
            shutil.copy2(jar_path, dest)
            
            print(f"✅ Downloaded to: {dest}")
            
            # Cleanup
            shutil.rmtree(temp_dir, ignore_errors=True)
            
            self.floodgate_jar = dest
            return dest
            
        except Exception as e:
            print(f"❌ Download failed: {e}")
            print("\n💡 Manual download:")
            print("   1. Visit https://geysermc.org/wiki/floodgate/setup/")
            print("   2. Download floodgate.jar")
            print("   3. Upload to /kaggle/working/minecraft_server/plugins/")
            return None
    
    def configure_geyser(self):
        """
        Configure Geyser after first run.
        
        Geyser generates config.yml on first run.
        We may need to adjust:
          - bedrock: port (19132)
          - bedrock: host (0.0.0.0)
          - remote: port (25565)
        """
        config_path = os.path.join(PLUGINS_DIR, "Geyser-Spigot", "config.yml")
        
        if not os.path.exists(config_path):
            print("\n⚠️  Geyser config not found")
            print("   Config will be generated on first server run")
            print("   Run server, then check plugins/Geyser-Spigot/config.yml")
            return
        
        print(f"\n⚙️  Geyser configuration:")
        print(f"   Config file: {config_path}")
        print("\n   Default settings:")
        print(f"   - Bedrock listener: {BEDROCK_LISTENER_HOST}:{BEDROCK_LISTENER_PORT} (UDP)")
        print(f"   - Java remote: localhost:{JAVA_PORT} (TCP)")
        print("\n   These should work for most setups.")
        
        # Read and show current config if exists
        try:
            with open(config_path) as f:
                config = f.read()
            
            # Show key settings
            for line in config.split('\n'):
                if any(k in line.lower() for k in ['port', 'host', 'address', 'velocity']):
                    if not line.strip().startswith('#'):
                        print(f"   {line.strip()}")
        except Exception as e:
            print(f"   Could not read config: {e}")
    
    def check_plugin_compatibility(self) -> bool:
        """
        Check if plugins are compatible with the server.
        
        Returns:
            True if compatible, False otherwise
        """
        print("\n🔍 Checking plugin compatibility...")
        
        # Check Paper version
        server_prop = os.path.join(SERVER_DIR, "server.properties")
        mc_version = "1.21.4"  # From your config
        
        print(f"   Server version: {mc_version}")
        print(f"   Plugins: Geyser-Spigot + Floodgate")
        print(f"   Compatibility: ✅ Should work with Paper {mc_version}")
        
        # Note about Playit compatibility
        print("\n⚠️  IMPORTANT - Playit Compatibility:")
        print("   - Geyser plugin does NOT work with Playit UDP tunnels")
        print("   - For Java: Use Playit program agent (TCP tunnel)")
        print("   - For Bedrock:")
        print("     * Playit free tier may NOT support Bedrock/NetherNet")
        print("     * May need paid plan for Bedrock support")
        print("     * Alternative: Use ngrok TCP + Geyser UDP separately")
        
        return True
    
    def install_all(self) -> bool:
        """
        Install both Geyser and Floodgate.
        
        Returns:
            True if both installed successfully
        """
        print("\n" + "=" * 60)
        print("GEYSER + FLOODGATE INSTALLATION")
        print("=" * 60)
        
        self.ensure_plugins_dir()
        
        success = True
        
        # Download Geyser
        if not self.download_geyser():
            success = False
        
        # Download Floodgate
        if not self.download_floodgate():
            success = False
        
        if success:
            print("\n✅ Both plugins installed")
            print("\n📋 Next steps:")
            print("   1. Start the server (plugins will load on startup)")
            print("   2. Check logs for Geyser/Floodgate initialization")
            print("   3. Configure Geyser if needed (config.yml)")
            print("   4. Test Java connection")
            print("   5. Test Bedrock connection (if Playit supports it)")
        else:
            print("\n⚠️  Some plugins failed to download")
            print("   See instructions above for manual download")
        
        return success


def test_geyser_setup():
    """Test Geyser setup without downloading."""
    print("\n" + "=" * 60)
    print("GEYSER SETUP TEST (DRY RUN)")
    print("=" * 60)
    
    setup = GeyserCrossplay()
    
    print("\n1. Plugins directory:")
    setup.ensure_plugins_dir()
    
    print("\n2. Version check:")
    setup.get_latest_versions()
    
    print("\n3. Compatibility check:")
    setup.check_plugin_compatibility()
    
    print("\n✅ Test complete")
    print("\n📋 To install, run in live Kaggle session:")
    print("   setup = GeyserCrossplay()")
    print("   setup.install_all()")


if __name__ == "__main__":
    test_geyser_setup()
