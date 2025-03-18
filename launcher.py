#!/usr/bin/env python
import os
import json
import subprocess
import shutil
import platform
import sys
import time
import re
import requests
from colorama import init, Fore, Back, Style
from tqdm import tqdm
from asset_downloader import AssetDownloader
from library_manager import LibraryManager
import threading
import uuid
import hashlib
import signal
import psutil
from datetime import datetime

# Initialize colorama
init(autoreset=True)

# Constants
CONFIG_FILE = "config.json"
SOURCES_DIR = "sources"
VERSIONS_DIR = os.path.join(SOURCES_DIR, "versions")
ASSETS_DIR = os.path.join(SOURCES_DIR, "assets")
LIBRARIES_DIR = os.path.join(SOURCES_DIR, "libraries")
MINECRAFT_DIR = os.path.join(os.path.expanduser("~"), ".minecraft")
MANIFEST_PATH = os.path.join(SOURCES_DIR, "version_manifest.json")

class MinecraftLauncher:
    def __init__(self):
        self.config = self.load_config()
        self.setup_directories()
        self.version_manifest = None
        self.asset_downloader = AssetDownloader(ASSETS_DIR)
        self.library_manager = LibraryManager(LIBRARIES_DIR)
        self.available_java_versions = []
        
        # Update thread count based on config
        self.asset_downloader.max_workers = self.config["download_threads"]
        self.library_manager.max_workers = self.config["download_threads"]
        
    def setup_directories(self):
        """Create necessary directories if they don't exist"""
        directories = [SOURCES_DIR, VERSIONS_DIR, ASSETS_DIR, LIBRARIES_DIR]
        for directory in directories:
            if not os.path.exists(directory):
                os.makedirs(directory)
                
        # Create Minecraft directory if it doesn't exist
        if not os.path.exists(MINECRAFT_DIR):
            os.makedirs(MINECRAFT_DIR)
    
    def load_config(self):
        """Load or create configuration file"""
        default_config = {
            "username": "Player",
            "ram": 2,  # GB
            "java_path": "",
            "game_directory": MINECRAFT_DIR,
            "resolution": {
                "width": 854,
                "height": 480
            },
            "last_version": "",
            "download_threads": min(32, os.cpu_count() * 4),  # Default to CPU-based threads
            "preferred_java_version": "",  # New setting for Java version
            "java_versions": {}  # Store detected Java versions
        }
        
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    # Add new config options if they don't exist (for upgrades)
                    for key, value in default_config.items():
                        if key not in config:
                            config[key] = value
                    return config
            except (json.JSONDecodeError, FileNotFoundError):
                return default_config
        else:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(default_config, f, indent=4)
            return default_config
    
    def save_config(self):
        """Save configuration to file"""
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f, indent=4)
    
    def get_version_manifest(self):
        """Fetch the version manifest from Mojang"""
        try:
            print(f"{Fore.YELLOW}Fetching version list from Mojang...")
            response = requests.get("https://launchermeta.mojang.com/mc/game/version_manifest.json")
            response.raise_for_status()
            self.version_manifest = response.json()
            
            # Save manifest to file
            os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)
            with open(MANIFEST_PATH, 'w') as f:
                json.dump(self.version_manifest, f)
            
            return True
        except requests.RequestException as e:
            print(f"{Fore.RED}Error fetching version list: {e}")
            return False
    
    def list_versions(self):
        """List available Minecraft versions"""
        if not self.version_manifest:
            if not self.get_version_manifest():
                return []
        
        return [(v["id"], v["type"], v["releaseTime"][:10]) for v in self.version_manifest["versions"]]
    
    def download_version(self, version_id):
        """Download a specific Minecraft version"""
        if not self.version_manifest:
            if not self.get_version_manifest():
                return False
        
        # Find the version in the manifest
        version_info = None
        for v in self.version_manifest["versions"]:
            if v["id"] == version_id:
                version_info = v
                break
        
        if not version_info:
            print(f"{Fore.RED}Version {version_id} not found!")
            return False
        
        # Create version directory
        version_dir = os.path.join(VERSIONS_DIR, version_id)
        if not os.path.exists(version_dir):
            os.makedirs(version_dir)
        
        # Download version JSON
        try:
            print(f"{Fore.YELLOW}Downloading {version_id} metadata...")
            response = requests.get(version_info["url"])
            response.raise_for_status()
            version_data = response.json()
            
            # Save version JSON
            version_json_path = os.path.join(version_dir, f"{version_id}.json")
            with open(version_json_path, 'w') as f:
                json.dump(version_data, f, indent=4)
            
            # Download the client JAR
            client_url = version_data["downloads"]["client"]["url"]
            client_size = version_data["downloads"]["client"]["size"]
            client_path = os.path.join(version_dir, f"{version_id}.jar")
            
            # Skip if the JAR already exists and has the right size
            if os.path.exists(client_path) and os.path.getsize(client_path) == client_size:
                print(f"{Fore.GREEN}Client JAR already exists, skipping download.")
            else:
                print(f"{Fore.YELLOW}Downloading {version_id} client.jar ({client_size//1024//1024} MB)...")
                
                # Setup headers for faster downloads
                headers = {
                    'User-Agent': 'Echo-Launcher/1.0',
                    'Accept-Encoding': 'gzip, deflate',
                    'Connection': 'keep-alive'
                }
                
                # Use a session for connection pooling
                with requests.Session() as session:
                    # Configure session for better performance
                    session.headers.update(headers)
                    
                    # Start the request with streaming
                    response = session.get(client_url, stream=True)
                    response.raise_for_status()
                    
                    total_size = int(response.headers.get('content-length', 0))
                    block_size = 1024 * 1024  # 1 MB chunks for better performance
                    
                    # Create a read-ahead buffer for improved throughput
                    buffer_size = min(20 * 1024 * 1024, total_size // 5)  # 20MB buffer or 1/5 of total size
                    read_ahead_buffer = []
                    buffer_event = threading.Event()
                    download_complete = threading.Event()
                    buffer_lock = threading.Lock()
                    
                    def buffer_reader():
                        """Read data into buffer ahead of consumption"""
                        try:
                            for chunk in response.iter_content(block_size):
                                with buffer_lock:
                                    read_ahead_buffer.append(chunk)
                                buffer_event.set()  # Signal data is available
                                
                                # Wait if buffer is too full
                                while len(read_ahead_buffer) * block_size > buffer_size and not download_complete.is_set():
                                    time.sleep(0.1)
                                    
                                if download_complete.is_set():
                                    break
                        except Exception as e:
                            print(f"{Fore.RED}Error in read-ahead buffer: {e}")
                            buffer_event.set()  # Signal to prevent deadlock
                    
                    # Start buffer thread
                    buffer_thread = threading.Thread(target=buffer_reader)
                    buffer_thread.daemon = True
                    buffer_thread.start()
                    
                    with open(client_path, 'wb') as f, tqdm(
                        desc=f"{version_id}.jar", 
                        total=total_size,
                        unit='B',
                        unit_scale=True,
                        unit_divisor=1024,
                        bar_format="{l_bar}%s{bar}%s{r_bar}" % (Fore.GREEN, Fore.RESET)
                    ) as t:
                        bytes_written = 0
                        while bytes_written < total_size:
                            # Wait for data to be available
                            if not read_ahead_buffer:
                                buffer_event.wait()
                                buffer_event.clear()
                            
                            # Get chunk from buffer
                            chunk = None
                            with buffer_lock:
                                if read_ahead_buffer:
                                    chunk = read_ahead_buffer.pop(0)
                            
                            if chunk:
                                f.write(chunk)
                                bytes_written += len(chunk)
                                t.update(len(chunk))
                            elif bytes_written >= total_size:
                                break
                    
                    # Signal buffer thread to exit
                    download_complete.set()
                    buffer_thread.join(timeout=1.0)
            
            # Download libraries
            self.library_manager.download_libraries(version_data)
            
            # Download asset index and assets
            asset_index_id = self.asset_downloader.download_asset_index(version_data)
            if asset_index_id:
                self.asset_downloader.download_assets(asset_index_id)
                
            # Extract native libraries
            self.extract_natives(version_data, version_dir)
            
            # Update last used version
            self.config["last_version"] = version_id
            self.save_config()
            
            print(f"{Fore.GREEN}Successfully downloaded {version_id}!")
            return True
            
        except requests.RequestException as e:
            print(f"{Fore.RED}Error downloading version: {e}")
            return False
    
    def extract_natives(self, version_data, version_dir):
        """Extract native libraries for the Minecraft version"""
        if "libraries" not in version_data:
            return
        
        # Create natives directory
        natives_dir = os.path.join(version_dir, "natives")
        if not os.path.exists(natives_dir):
            os.makedirs(natives_dir)
        
        print(f"{Fore.YELLOW}Extracting native libraries...")
        
        # Get current OS
        current_os = platform.system().lower()
        os_mapping = {
            "windows": "windows",
            "linux": "linux",
            "darwin": "osx"  # macOS
        }
        os_key = os_mapping.get(current_os, current_os)
        
        # Get architecture (32 or 64 bit)
        arch = "64" if platform.architecture()[0] == "64bit" else "32"
        
        # Process libraries
        for library in version_data["libraries"]:
            # Skip libraries without native entries
            if "natives" not in library:
                continue
            
            # Skip libraries not for this OS
            if os_key not in library["natives"]:
                continue
            
            # Check rules if present
            if "rules" in library:
                allowed = False
                for rule in library["rules"]:
                    action = rule.get("action", "allow") == "allow"
                    
                    # If no OS is specified, this rule applies to all OSes
                    if "os" not in rule:
                        allowed = action
                        continue
                    
                    os_name = rule["os"].get("name")
                    os_version = rule["os"].get("version")
                    
                    if os_name and os_name.lower() == current_os:
                        if os_version and not re.search(os_version, platform.version()):
                            continue
                        allowed = action
                
                if not allowed:
                    continue
            
            # Get the classified name (replace ${arch} with actual architecture)
            classifier = library["natives"][os_key].replace("${arch}", arch)
            
            # Find and extract the native library
            if "downloads" in library and "classifiers" in library["downloads"] and classifier in library["downloads"]["classifiers"]:
                native_info = library["downloads"]["classifiers"][classifier]
                native_path = os.path.join(LIBRARIES_DIR, native_info["path"])
                
                # Check if file exists and matches expected size
                if os.path.exists(native_path) and os.path.getsize(native_path) == native_info["size"]:
                    # Extract native library
                    self.extract_native_jar(native_path, natives_dir)
                else:
                    print(f"{Fore.RED}Native library missing or corrupted: {native_path}")
            else:
                # Manually construct the path if not provided
                if "name" in library:
                    parts = library["name"].split(":")
                    if len(parts) >= 3:
                        group_id, artifact_id, version = parts[:3]
                        group_path = group_id.replace(".", "/")
                        filename = f"{artifact_id}-{version}-{classifier}.jar"
                        path = f"{group_path}/{artifact_id}/{version}/{filename}"
                        native_path = os.path.join(LIBRARIES_DIR, path)
                        
                        if os.path.exists(native_path):
                            self.extract_native_jar(native_path, natives_dir)
        
        print(f"{Fore.GREEN}Native libraries extracted to {natives_dir}")
    
    def extract_native_jar(self, jar_path, output_dir):
        """Extract a JAR file containing native libraries"""
        import zipfile
        
        try:
            with zipfile.ZipFile(jar_path, 'r') as zip_ref:
                # Get list of files to extract
                for file_info in zip_ref.infolist():
                    filename = file_info.filename
                    
                    # Skip directories and unwanted files
                    if filename.endswith('/') or '__MACOSX' in filename:
                        continue
                    
                    # Skip META-INF and other non-library files
                    if filename.startswith('META-INF/') or not self.is_native_library(filename):
                        continue
                    
                    # Extract the file
                    output_path = os.path.join(output_dir, os.path.basename(filename))
                    
                    # Extract only if file doesn't exist or is outdated
                    if not os.path.exists(output_path) or os.path.getmtime(jar_path) > os.path.getmtime(output_path):
                        with zip_ref.open(file_info) as source, open(output_path, 'wb') as target:
                            target.write(source.read())
        except (zipfile.BadZipFile, FileNotFoundError, PermissionError) as e:
            print(f"{Fore.RED}Error extracting native library {jar_path}: {e}")
    
    def is_native_library(self, filename):
        """Check if a file is a native library"""
        extensions = {
            'windows': ['.dll'],
            'linux': ['.so'],
            'darwin': ['.dylib', '.jnilib']
        }
        
        current_os = platform.system().lower()
        os_key = current_os
        if current_os == 'darwin':
            os_key = 'darwin'
        
        if os_key in extensions:
            return any(filename.lower().endswith(ext) for ext in extensions[os_key])
        
        return False
    
    def get_installed_versions(self):
        """Get list of locally installed versions"""
        if not os.path.exists(VERSIONS_DIR):
            return []
        
        versions = []
        for version_id in os.listdir(VERSIONS_DIR):
            version_dir = os.path.join(VERSIONS_DIR, version_id)
            if os.path.isdir(version_dir):
                jar_file = os.path.join(version_dir, f"{version_id}.jar")
                json_file = os.path.join(version_dir, f"{version_id}.json")
                if os.path.exists(jar_file) and os.path.exists(json_file):
                    versions.append(version_id)
        
        return versions
    
    def build_classpath(self, version_id, version_data):
        """Build the classpath for the given version"""
        classpath = []
        
        # Add the client JAR
        client_jar = os.path.join(VERSIONS_DIR, version_id, f"{version_id}.jar")
        classpath.append(client_jar)
        
        # Add libraries
        if "libraries" in version_data:
            libraries = version_data["libraries"]
            current_os = platform.system().lower()
            
            for library in libraries:
                # Skip libraries that don't apply to the current OS
                if "rules" in library:
                    allowed = False
                    for rule in library["rules"]:
                        action = rule.get("action", "allow") == "allow"
                        
                        # If no OS is specified, this rule applies to all OSes
                        if "os" not in rule:
                            allowed = action
                            continue
                        
                        os_name = rule["os"].get("name")
                        os_version = rule["os"].get("version")
                        
                        if os_name and os_name.lower() == current_os:
                            if os_version and not re.search(os_version, platform.version()):
                                continue
                            allowed = action
                    
                    if not allowed:
                        continue
                
                # Get the path for the library
                if "downloads" in library and "artifact" in library["downloads"]:
                    artifact = library["downloads"]["artifact"]
                    path = artifact.get("path")
                    if path:
                        classpath.append(os.path.join(LIBRARIES_DIR, path))
                
                # Manually construct path if not provided
                if "name" in library and "downloads" not in library:
                    parts = library["name"].split(":")
                    if len(parts) >= 3:
                        group_id, artifact_id, version = parts[:3]
                        group_path = group_id.replace(".", "/")
                        filename = f"{artifact_id}-{version}.jar"
                        path = f"{group_path}/{artifact_id}/{version}/{filename}"
                        classpath.append(os.path.join(LIBRARIES_DIR, path))
        
        return classpath
    
    def detect_java_versions(self):
        """Detect installed Java versions and their paths"""
        java_versions = {}
        self.available_java_versions = []
        
        if platform.system() == "Windows":
            # Check common installation paths for Java
            possible_paths = [
                os.path.join(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "Java"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "Java"),
                os.path.join("C:\\Program Files", "Eclipse Adoptium"),
                os.path.join("C:\\Program Files", "Eclipse Foundation"),
                os.path.join("C:\\Program Files", "BellSoft"),
                os.path.join("C:\\Program Files", "Microsoft"),
                os.path.join("C:\\Program Files", "Zulu"),
                os.path.join("C:\\Program Files", "Amazon Corretto"),
                os.path.join("C:\\Program Files", "RedHat"),
                os.path.join("C:\\Program Files", "Semeru"),
                os.path.join("C:\\Program Files", "LibericaJDK")
            ]
            
            # Look for Java in each possible path
            for path in possible_paths:
                if os.path.exists(path):
                    for folder in os.listdir(path):
                        java_exe = os.path.join(path, folder, "bin", "java.exe")
                        if os.path.exists(java_exe):
                            # Try to get Java version
                            try:
                                java_version_info = subprocess.check_output([java_exe, "-version"], stderr=subprocess.STDOUT, text=True, encoding='utf-8')
                                if "version" in java_version_info:
                                    version_string = java_version_info.split("\n")[0]
                                    
                                    # Extract major version
                                    major_version = None
                                    if "1." in version_string:
                                        # Old versioning scheme (1.8)
                                        major_version = int(version_string.split("1.")[1].split(".")[0])
                                    else:
                                        # New versioning scheme (9+)
                                        import re
                                        match = re.search(r'(\d+)', version_string)
                                        if match:
                                            major_version = int(match.group(1))
                                    
                                    if major_version:
                                        java_versions[f"Java {major_version} ({folder})"] = {
                                            "path": java_exe,
                                            "version": major_version,
                                            "version_string": version_string
                                        }
                                        self.available_java_versions.append(f"Java {major_version} ({folder})")
                            except (subprocess.SubprocessError, ValueError, IndexError):
                                # Failed to get version, skip this installation
                                pass
            
            # Also check system PATH
            try:
                java_path = shutil.which("java")
                if java_path:
                    try:
                        java_version_info = subprocess.check_output([java_path, "-version"], stderr=subprocess.STDOUT, text=True, encoding='utf-8')
                        if "version" in java_version_info:
                            version_string = java_version_info.split("\n")[0]
                            
                            # Extract major version
                            major_version = None
                            if "1." in version_string:
                                major_version = int(version_string.split("1.")[1].split(".")[0])
                            else:
                                import re
                                match = re.search(r'(\d+)', version_string)
                                if match:
                                    major_version = int(match.group(1))
                            
                            if major_version:
                                java_versions[f"Java {major_version} (Default)"] = {
                                    "path": java_path,
                                    "version": major_version,
                                    "version_string": version_string
                                }
                                self.available_java_versions.append(f"Java {major_version} (Default)")
                    except (subprocess.SubprocessError, ValueError, IndexError):
                        # Failed to get version
                        pass
            except:
                # Failed to get Java from PATH
                pass
        else:
            # For Linux and macOS
            try:
                java_path = shutil.which("java")
                if java_path:
                    try:
                        java_version_info = subprocess.check_output([java_path, "-version"], stderr=subprocess.STDOUT, text=True, encoding='utf-8')
                        if "version" in java_version_info:
                            version_string = java_version_info.split("\n")[0]
                            
                            # Extract major version
                            major_version = None
                            if "1." in version_string:
                                major_version = int(version_string.split("1.")[1].split(".")[0])
                            else:
                                import re
                                match = re.search(r'(\d+)', version_string)
                                if match:
                                    major_version = int(match.group(1))
                            
                            if major_version:
                                java_versions[f"Java {major_version} (Default)"] = {
                                    "path": java_path,
                                    "version": major_version,
                                    "version_string": version_string
                                }
                                self.available_java_versions.append(f"Java {major_version} (Default)")
                    except (subprocess.SubprocessError, ValueError, IndexError):
                        # Failed to get version
                        pass
            except:
                # Failed to get Java from PATH
                pass
        
        # Save detected Java versions to config
        self.config["java_versions"] = java_versions
        self.save_config()
        
        return java_versions
    
    def get_recommended_java_version(self, minecraft_version):
        """Get the recommended Java version for a Minecraft version"""
        # Minecraft 1.17+ requires Java 16+
        # Minecraft 1.18+ requires Java 17+
        # Minecraft 1.20.5+ requires Java 21+
        try:
            mc_version_parts = minecraft_version.split(".")
            major = int(mc_version_parts[0])
            minor = int(mc_version_parts[1])
            patch = 0
            if len(mc_version_parts) > 2:
                try:
                    patch = int(mc_version_parts[2].split("-")[0])  # Handle versions like "1.16.5-pre1"
                except ValueError:
                    patch = 0
            
            if major == 1:
                if minor >= 20 and patch >= 5:
                    return 21
                elif minor >= 18:
                    return 17
                elif minor >= 17:
                    return 16
                elif minor <= 16:
                    # For old versions, Java 8 is best for compatibility
                    return 8
                else:
                    return 8
        except:
            # If can't parse version, default to Java 8
            return 8
        
        return 8
    
    def select_java_for_version(self, minecraft_version):
        """Select an appropriate Java version for the specified Minecraft version"""
        if not self.available_java_versions:
            self.detect_java_versions()
        
        # If user has a preferred Java version, use that
        if self.config["preferred_java_version"] and self.config["preferred_java_version"] in self.config["java_versions"]:
            return self.config["java_versions"][self.config["preferred_java_version"]]["path"]
        
        # Get recommended version
        recommended_version = self.get_recommended_java_version(minecraft_version)
        
        # Find the best match (exact or higher version)
        best_match = None
        best_version = 0
        
        for java_name, java_info in self.config["java_versions"].items():
            java_version = java_info["version"]
            
            # If exact match, use it
            if java_version == recommended_version:
                return java_info["path"]
            
            # If higher version, record it as potential match
            if java_version > recommended_version and (best_version == 0 or java_version < best_version):
                best_version = java_version
                best_match = java_info["path"]
        
        # If found a higher version, use it
        if best_match:
            return best_match
        
        # Fall back to default Java path
        return self.config["java_path"] or "java"
    
    def launch_game(self, version_id):
        """Launch the game with the specified version"""
        version_dir = os.path.join(VERSIONS_DIR, version_id)
        jar_file = os.path.join(version_dir, f"{version_id}.jar")
        json_file = os.path.join(version_dir, f"{version_id}.json")
        
        if not os.path.exists(jar_file):
            print(f"{Fore.RED}Version {version_id} is not installed!")
            return False
            
        if not os.path.exists(json_file):
            print(f"{Fore.RED}Version JSON not found for {version_id}!")
            return False
            
        # Load version JSON to get the asset index ID and build classpath
        try:
            with open(json_file, 'r') as f:
                version_data = json.load(f)
                
            asset_index_id = version_data["assetIndex"]["id"]
            main_class = version_data.get("mainClass", "net.minecraft.client.main.Main")
            
            # Get Java version requirement from version_data if available
            java_version_requirement = None
            if "javaVersion" in version_data:
                java_version_requirement = version_data["javaVersion"].get("majorVersion", None)
            else:
                # For older versions that don't specify Java version
                java_version_requirement = self.get_recommended_java_version(version_id)
        except (json.JSONDecodeError, KeyError, FileNotFoundError):
            print(f"{Fore.YELLOW}Warning: Could not determine version details, using defaults.")
            asset_index_id = "1.19"  # Default fallback
            main_class = "net.minecraft.client.main.Main"
            java_version_requirement = self.get_recommended_java_version(version_id)
        
        # Build classpath
        try:
            classpath = self.build_classpath(version_id, version_data)
            classpath_str = os.pathsep.join(classpath)
        except Exception as e:
            print(f"{Fore.YELLOW}Warning: Error building classpath: {e}. Using default.")
            classpath_str = jar_file
        
        # Select appropriate Java version
        java_path = self.select_java_for_version(version_id)
        
        # If Java requirement is specified in the version and we have detected versions
        if java_version_requirement and self.config["java_versions"]:
            # Check if the selected Java version meets the requirement
            selected_version_key = None
            for java_key, java_info in self.config["java_versions"].items():
                if java_info["path"] == java_path:
                    selected_version_key = java_key
                    if java_info["version"] < java_version_requirement:
                        print(f"{Fore.YELLOW}Warning: This Minecraft version requires Java {java_version_requirement}+, but the selected Java version is {java_info['version']}.")
                        print(f"{Fore.YELLOW}This may cause the game to crash. Would you like to select a different Java version?")
                        choice = input(f"{Fore.CYAN}Select a different Java version? (y/n): ").strip().lower()
                        if choice == "y":
                            java_path = self.java_version_menu(required_version=java_version_requirement)
                    break
        
        # Ensure natives are extracted
        natives_dir = os.path.join(version_dir, "natives")
        if not os.path.exists(natives_dir) or not os.listdir(natives_dir):
            print(f"{Fore.YELLOW}Native libraries not found. Extracting now...")
            self.extract_natives(version_data, version_dir)
        
        # Build command
        ram_mb = int(self.config["ram"] * 1024)  # Convert to integer to avoid decimal values
        username = self.config["username"]
        width = self.config["resolution"]["width"]
        height = self.config["resolution"]["height"]
        
        # Generate a valid UUID for offline mode
        # Create a deterministic UUID based on the username
        name_hash = hashlib.md5(username.encode('utf-8')).digest()
        offline_uuid = str(uuid.UUID(bytes=name_hash[:16]))
        # Alternative method: random UUID
        # offline_uuid = str(uuid.uuid4())
        
        cmd = [
            java_path,
            f"-Xmx{ram_mb}M",
            "-XX:+UnlockExperimentalVMOptions",
            "-XX:+UseG1GC",
            "-XX:G1NewSizePercent=20",
            "-XX:G1ReservePercent=20",
            "-XX:MaxGCPauseMillis=50",
            "-XX:G1HeapRegionSize=32M",
            # Add native library path
            f"-Djava.library.path={natives_dir}",
            "-Dminecraft.launcher.brand=EchoLauncher",
            "-cp", classpath_str,
            main_class,
            "--username", username,
            "--version", version_id,
            "--gameDir", self.config["game_directory"],
            "--assetsDir", os.path.abspath(ASSETS_DIR),
            "--assetIndex", asset_index_id,
            "--uuid", offline_uuid,
            "--accessToken", "0",  # Offline mode token
            "--width", str(width),
            "--height", str(height),
            "--userType", "legacy"
        ]
        
        try:
            print(f"{Fore.GREEN}Launching Minecraft {version_id}...")
            
            # Launch with output capture for error detection
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
            
            # Wait briefly to catch immediate errors
            try:
                return_code = process.wait(timeout=3)
                if return_code != 0:
                    # Something went wrong immediately
                    stderr = process.stderr.read()
                    self.handle_launch_error(stderr, version_id, java_version_requirement)
                    return False
            except subprocess.TimeoutExpired:
                # No immediate errors, game is launching
                # Create game monitor
                monitor = GameMonitor(process, version_id, username, java_path)
                monitor.start_monitoring()
                
                # Display game status window
                monitor.display_status()
                
            return True
        except Exception as e:
            print(f"{Fore.RED}Error launching game: {e}")
            return False
    
    def handle_launch_error(self, error_text, minecraft_version, required_java_version=None):
        """Handle common launch errors and provide solutions"""
        print(f"{Fore.RED}Error launching Minecraft!")
        
        # Check for common error patterns
        java_version_error = "UnsupportedClassVersionError" in error_text
        java_too_old = "has been compiled by a more recent version of the Java Runtime" in error_text
        class_not_found = "ClassNotFoundException" in error_text
        no_main_class = "Could not find or load main class" in error_text
        out_of_memory = "OutOfMemoryError" in error_text
        uuid_error = "Invalid UUID string" in error_text
        heap_size_error = "Invalid maximum heap size" in error_text
        lwjgl_error = "Failed to locate library: lwjgl" in error_text or "UnsatisfiedLinkError" in error_text
        
        if lwjgl_error:
            print(f"{Fore.YELLOW}LWJGL native library error detected. This is usually caused by missing or corrupted native libraries.")
            print(f"{Fore.CYAN}Attempting to fix the issue by re-extracting native libraries...")
            
            try:
                # Re-extract native libraries
                version_dir = os.path.join(VERSIONS_DIR, minecraft_version)
                json_file = os.path.join(version_dir, f"{minecraft_version}.json")
                
                if os.path.exists(json_file):
                    with open(json_file, 'r') as f:
                        version_data = json.load(f)
                    
                    # Clean up old natives directory
                    natives_dir = os.path.join(version_dir, "natives")
                    if os.path.exists(natives_dir):
                        print(f"{Fore.YELLOW}Removing old native libraries...")
                        
                        # Try to delete files in the directory, but handle if files are locked
                        for filename in os.listdir(natives_dir):
                            file_path = os.path.join(natives_dir, filename)
                            try:
                                if os.path.isfile(file_path):
                                    os.unlink(file_path)
                            except Exception as e:
                                print(f"{Fore.RED}Could not remove {file_path}: {e}")
                    
                    # Extract natives again
                    self.extract_natives(version_data, version_dir)
                    
                    print(f"{Fore.GREEN}Native libraries have been re-extracted.")
                    print(f"{Fore.CYAN}Would you like to try launching the game again?")
                    if input(f"{Fore.CYAN}Launch {minecraft_version} now? (y/n): ").strip().lower() == 'y':
                        return self.launch_game(minecraft_version)
                else:
                    print(f"{Fore.RED}Could not find version data. Try re-downloading the version.")
            except Exception as e:
                print(f"{Fore.RED}Error fixing native libraries: {e}")
                print(f"{Fore.YELLOW}Possible alternative solutions:")
                print(f"{Fore.YELLOW}1. Try re-downloading this Minecraft version")
                print(f"{Fore.YELLOW}2. Make sure your antivirus isn't blocking Java or the launcher")
                print(f"{Fore.YELLOW}3. Check that your graphics drivers are up to date")
        elif heap_size_error:
            print(f"{Fore.YELLOW}Invalid heap size error detected. This is usually caused by a decimal value in RAM allocation.")
            current_ram = self.config["ram"]
            print(f"{Fore.CYAN}Current allocation: {current_ram} GB")
            try:
                new_ram = int(float(input(f"{Fore.CYAN}Enter new RAM allocation in GB (whole numbers recommended, e.g. 2): ").strip()))
                if new_ram > 0:
                    self.config["ram"] = new_ram
                    self.save_config()
                    print(f"{Fore.GREEN}RAM allocation updated to {new_ram} GB.")
                    
                    print(f"{Fore.CYAN}Would you like to try launching the game again?")
                    if input(f"{Fore.CYAN}Launch {minecraft_version} now? (y/n): ").strip().lower() == 'y':
                        return self.launch_game(minecraft_version)
                else:
                    print(f"{Fore.RED}Invalid value. RAM allocation unchanged.")
            except ValueError:
                print(f"{Fore.RED}Invalid input. RAM allocation unchanged.")
        elif uuid_error:
            print(f"{Fore.YELLOW}UUID format error detected. This has been fixed in the latest update.")
            print(f"{Fore.YELLOW}Please try launching again, or restart the launcher if the problem persists.")
            
            # Attempt to fix common UUID issues by regenerating the config file
            try:
                backup_config = f"{CONFIG_FILE}.bak"
                print(f"{Fore.YELLOW}Creating backup of your config file as {backup_config}")
                if os.path.exists(CONFIG_FILE):
                    shutil.copy2(CONFIG_FILE, backup_config)
                
                # Get current config
                config = self.config.copy()
                
                # Save with renewed format
                with open(CONFIG_FILE, 'w') as f:
                    json.dump(config, f, indent=4)
                
                print(f"{Fore.GREEN}Config file has been refreshed.")
            except Exception as e:
                print(f"{Fore.RED}Error fixing config: {e}")
        
        elif java_version_error or java_too_old:
            print(f"{Fore.YELLOW}This error indicates that your Java version is too old for this Minecraft version.")
            
            if "class file version 65.0" in error_text:
                print(f"{Fore.YELLOW}This Minecraft version requires Java 21 or newer.")
                rec_version = 21
            elif "class file version 61.0" in error_text:
                print(f"{Fore.YELLOW}This Minecraft version requires Java 17 or newer.")
                rec_version = 17
            elif "class file version 60.0" in error_text:
                print(f"{Fore.YELLOW}This Minecraft version requires Java 16 or newer.")
                rec_version = 16
            else:
                rec_version = required_java_version or self.get_recommended_java_version(minecraft_version)
                print(f"{Fore.YELLOW}This Minecraft version requires Java {rec_version} or newer.")
            
            print(f"{Fore.CYAN}Would you like to select a different Java version?")
            choice = input(f"{Fore.CYAN}Open Java version selector? (y/n): ").strip().lower()
            if choice == "y":
                self.java_version_menu(required_version=rec_version)
        
        elif class_not_found or no_main_class:
            print(f"{Fore.YELLOW}This error indicates missing game files or an incomplete download.")
            print(f"{Fore.CYAN}Try re-downloading the Minecraft version.")
            print(f"{Fore.CYAN}Would you like to delete and re-download this version?")
            choice = input(f"{Fore.CYAN}Re-download Minecraft {minecraft_version}? (y/n): ").strip().lower()
            if choice == "y":
                # Delete the version directory and redownload
                version_dir = os.path.join(VERSIONS_DIR, minecraft_version)
                try:
                    shutil.rmtree(version_dir)
                    print(f"{Fore.GREEN}Deleted version files. Starting download...")
                    self.download_version(minecraft_version)
                except Exception as e:
                    print(f"{Fore.RED}Error removing version: {e}")
        
        elif out_of_memory:
            print(f"{Fore.YELLOW}Java ran out of memory. Try increasing the RAM allocation.")
            current_ram = self.config["ram"]
            print(f"{Fore.CYAN}Current allocation: {current_ram} GB")
            try:
                new_ram = float(input(f"{Fore.CYAN}Enter new RAM allocation in GB (e.g. 4): ").strip())
                if new_ram > 0:
                    self.config["ram"] = new_ram
                    self.save_config()
                    print(f"{Fore.GREEN}RAM allocation updated to {new_ram} GB.")
                else:
                    print(f"{Fore.RED}Invalid value. RAM allocation unchanged.")
            except ValueError:
                print(f"{Fore.RED}Invalid input. RAM allocation unchanged.")
        
        else:
            print(f"{Fore.YELLOW}Unknown error. Full error message:")
            print(f"{Fore.RED}{error_text}")
            print(f"{Fore.CYAN}Possible solutions:")
            print(f"{Fore.CYAN}1. Try selecting a different Java version")
            print(f"{Fore.CYAN}2. Check your Minecraft installation")
            print(f"{Fore.CYAN}3. Make sure you have enough disk space and RAM")
            print(f"{Fore.CYAN}4. Check for conflicting software or antivirus blocking Java")
        
        input(f"\n{Fore.CYAN}Press Enter to return to the main menu...")
    
    def java_version_menu(self, required_version=None):
        """Display menu for selecting Java version"""
        clear_screen()
        print(f"{Fore.CYAN}{Style.BRIGHT}" + "═" * 60)
        print(f"{Fore.CYAN}{Style.BRIGHT}║ {Fore.WHITE}JAVA VERSION SELECTION")
        print(f"{Fore.CYAN}{Style.BRIGHT}" + "═" * 60)
        
        # Detect Java versions if not already done
        if not self.available_java_versions:
            print(f"{Fore.YELLOW}Detecting installed Java versions...")
            self.detect_java_versions()
        
        if not self.available_java_versions:
            print(f"{Fore.RED}No Java installations found! Please install Java and try again.")
            input(f"\n{Fore.CYAN}Press Enter to continue...")
            return self.config["java_path"] or "java"
        
        if required_version:
            print(f"{Fore.YELLOW}Required Java version: {required_version}+")
            print(f"{Fore.CYAN}{Style.BRIGHT}" + "─" * 60)
        
        # Display available Java versions
        for i, java_version in enumerate(self.available_java_versions, 1):
            version_info = self.config["java_versions"][java_version]
            version_str = version_info["version_string"]
            
            # Highlight if this is the current preferred version
            if java_version == self.config["preferred_java_version"]:
                print(f"{i}. {Fore.GREEN}{java_version} {Fore.BLUE}[{version_str}] {Fore.YELLOW}(Preferred)")
            # Highlight if this version meets the requirement
            elif required_version and version_info["version"] >= required_version:
                print(f"{i}. {Fore.GREEN}{java_version} {Fore.BLUE}[{version_str}] {Fore.GREEN}(Compatible)")
            # Show incompatible versions
            elif required_version and version_info["version"] < required_version:
                print(f"{i}. {Fore.RED}{java_version} {Fore.BLUE}[{version_str}] {Fore.RED}(Not compatible)")
            else:
                print(f"{i}. {Fore.GREEN}{java_version} {Fore.BLUE}[{version_str}]")
        
        print(f"{Fore.CYAN}{Style.BRIGHT}" + "─" * 60)
        print(f"C. {Fore.YELLOW}Custom Java path")
        print(f"R. {Fore.YELLOW}Rescan for Java installations")
        print(f"0. {Fore.RED}Back/Cancel")
        
        choice = input(f"\n{Fore.CYAN}Enter option: ").strip()
        
        if choice == "0":
            return self.config["java_path"] or "java"
        
        elif choice.lower() == "c":
            java_path = input(f"\n{Fore.YELLOW}Enter full Java executable path: ")
            if os.path.exists(java_path):
                self.config["java_path"] = java_path
                self.save_config()
                print(f"{Fore.GREEN}Java path updated!")
                return java_path
            else:
                print(f"{Fore.RED}Invalid Java path! Path does not exist.")
                input(f"\n{Fore.CYAN}Press Enter to continue...")
                return self.config["java_path"] or "java"
        
        elif choice.lower() == "r":
            self.detect_java_versions()
            return self.java_version_menu(required_version)
        
        elif choice.isdigit() and 1 <= int(choice) <= len(self.available_java_versions):
            selected_java = self.available_java_versions[int(choice) - 1]
            java_path = self.config["java_versions"][selected_java]["path"]
            
            # Set as preferred if compatible
            if not required_version or self.config["java_versions"][selected_java]["version"] >= required_version:
                set_preferred = input(f"\n{Fore.YELLOW}Set as preferred Java version for all Minecraft versions? (y/n): ").strip().lower()
                if set_preferred == "y":
                    self.config["preferred_java_version"] = selected_java
                    self.save_config()
                    print(f"{Fore.GREEN}Preferred Java version updated!")
            
            return java_path
        
        else:
            print(f"{Fore.RED}Invalid selection!")
            time.sleep(1)
            return self.java_version_menu(required_version)

    def change_settings(self):
        """Change launcher settings"""
        clear_screen()
        print(f"{Fore.CYAN}{Style.BRIGHT}" + "═" * 60)
        print(f"{Fore.CYAN}{Style.BRIGHT}║ {Fore.WHITE}SETTINGS")
        print(f"{Fore.CYAN}{Style.BRIGHT}" + "═" * 60)
        
        print(f"1. Username: {Fore.GREEN}{self.config['username']}")
        print(f"2. RAM Allocation: {Fore.GREEN}{self.config['ram']} GB")
        print(f"3. Resolution: {Fore.GREEN}{self.config['resolution']['width']}x{self.config['resolution']['height']}")
        print(f"4. Java Path: {Fore.GREEN}{self.config['java_path'] or 'Auto-detect'}")
        print(f"5. Game Directory: {Fore.GREEN}{self.config['game_directory']}")
        print(f"6. Download Threads: {Fore.GREEN}{self.config['download_threads']}")
        print(f"7. Java Version: {Fore.GREEN}{self.config['preferred_java_version'] or 'Auto-select'}")
        print(f"{Fore.CYAN}{Style.BRIGHT}" + "═" * 60)
        print(f"0. {Fore.YELLOW}Back to Main Menu")
        
        choice = input(f"\n{Fore.CYAN}Enter option: ")
        
        if choice == "1":
            username = input(f"\n{Fore.YELLOW}Enter new username: ")
            if username:
                self.config["username"] = username
                self.save_config()
                print(f"{Fore.GREEN}Username updated!")
        
        elif choice == "2":
            try:
                ram = float(input(f"\n{Fore.YELLOW}Enter RAM in GB (e.g. 2 or 4): "))
                if ram > 0:
                    self.config["ram"] = ram
                    self.save_config()
                    print(f"{Fore.GREEN}RAM allocation updated!")
                else:
                    print(f"{Fore.RED}Invalid value. Please enter a positive number.")
            except ValueError:
                print(f"{Fore.RED}Invalid input. Please enter a number.")
        
        elif choice == "3":
            try:
                width = int(input(f"\n{Fore.YELLOW}Enter width (e.g. 854): "))
                height = int(input(f"{Fore.YELLOW}Enter height (e.g. 480): "))
                if width > 0 and height > 0:
                    self.config["resolution"]["width"] = width
                    self.config["resolution"]["height"] = height
                    self.save_config()
                    print(f"{Fore.GREEN}Resolution updated!")
                else:
                    print(f"{Fore.RED}Invalid values. Please enter positive numbers.")
            except ValueError:
                print(f"{Fore.RED}Invalid input. Please enter numbers.")
        
        elif choice == "4":
            java_path = input(f"\n{Fore.YELLOW}Enter Java path (leave empty for auto-detect): ")
            self.config["java_path"] = java_path
            self.save_config()
            print(f"{Fore.GREEN}Java path updated!")
        
        elif choice == "5":
            game_dir = input(f"\n{Fore.YELLOW}Enter game directory (leave empty for default): ")
            if not game_dir:
                game_dir = MINECRAFT_DIR
            if os.path.exists(game_dir) or os.path.exists(os.path.dirname(game_dir)):
                self.config["game_directory"] = game_dir
                self.save_config()
                print(f"{Fore.GREEN}Game directory updated!")
            else:
                print(f"{Fore.RED}Directory does not exist!")
        
        elif choice == "6":
            try:
                threads = int(input(f"\n{Fore.YELLOW}Enter download threads (8-64 recommended, current CPU cores: {os.cpu_count()}): "))
                if threads > 0:
                    self.config["download_threads"] = threads
                    self.save_config()
                    # Update thread count for downloaders
                    self.asset_downloader.max_workers = threads
                    self.library_manager.max_workers = threads
                    print(f"{Fore.GREEN}Download threads updated!")
                else:
                    print(f"{Fore.RED}Invalid value. Please enter a positive number.")
            except ValueError:
                print(f"{Fore.RED}Invalid input. Please enter a number.")
        
        elif choice == "7":
            # Go to Java version selection menu
            self.java_version_menu()
            
        time.sleep(1)

class GameMonitor:
    def __init__(self, process, version_id, username, java_path):
        self.process = process
        self.version_id = version_id
        self.username = username
        self.java_path = java_path
        self.start_time = datetime.now()
        self.running = True
        self.monitoring_thread = None
    
    def start_monitoring(self):
        """Start monitoring the game process in a separate thread"""
        self.monitoring_thread = threading.Thread(target=self._monitor_process)
        self.monitoring_thread.daemon = True
        self.monitoring_thread.start()
    
    def _monitor_process(self):
        """Monitor the game process and update status"""
        while self.running:
            # Check if process is still running
            if self.process.poll() is not None:
                # Process has terminated
                self.running = False
                break
            
            # Sleep to avoid high CPU usage
            time.sleep(1)
    
    def display_status(self):
        """Display the game status window"""
        # Get runtime and memory information
        runtime_seconds = int(time.time() - self.start_time)
        hours, remainder = divmod(runtime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        runtime_str = f"{hours:02}:{minutes:02}:{seconds:02}"
        
        # Get memory usage
        try:
            process = psutil.Process(self.process.pid)
            memory_info = process.memory_info()
            memory_usage_mb = memory_info.rss / 1024 / 1024
            memory_percent = process.memory_percent()
            
            # Get CPU usage
            cpu_percent = process.cpu_percent(interval=0.1)
            
            # Get system memory info
            system_memory = psutil.virtual_memory()
            system_memory_percent = system_memory.percent
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            memory_usage_mb = 0
            memory_percent = 0
            cpu_percent = 0
            system_memory_percent = 0
        
        # Get terminal width for centering
        try:
            terminal_width = os.get_terminal_size().columns
        except:
            terminal_width = 100  # Default if can't determine
        
        menu_width = 60  # Width for centering
        margin = max(0, (terminal_width - menu_width) // 2)
        margin_space = " " * margin
        
        clear_screen()
        
        print()
        print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}🎮 MINECRAFT GAME STATUS MONITOR 🎮")
        print()
        
        # Game Information Section
        print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}GAME INFORMATION")
        print()
        
        # Version info with color
        version_display = f"Version: {Fore.GREEN}{self.version_id}"
        print(margin_space + f"{Fore.WHITE}{version_display}")
        
        # Player info with color
        player_display = f"Player: {Fore.GREEN}{self.username}"
        print(margin_space + f"{Fore.WHITE}{player_display}")
        
        # Java path info
        java_display = f"Java Path: {Fore.GREEN}{self.java_path}"
        print(margin_space + f"{Fore.WHITE}{java_display}")
        
        # Process ID info
        process_display = f"Process ID: {Fore.GREEN}{self.process.pid}"
        print(margin_space + f"{Fore.WHITE}{process_display}")
        
        print()
        
        # Runtime Statistics Section
        print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}RUNTIME STATISTICS")
        print()
        
        # Runtime info with bold time
        runtime_display = f"Runtime: {Fore.GREEN}{Style.BRIGHT}{runtime_str}{Fore.RESET}"
        print(margin_space + f"{Fore.WHITE}{runtime_display}")
        
        # Memory usage with visual bar
        memory_display = f"Memory: {Fore.GREEN}{int(memory_usage_mb)} MB ({int(memory_percent)}%)"
        
        # Create a visual bar for memory usage
        bar_length = 40
        filled_length = int(bar_length * memory_percent / 100)
        bar = f"{Fore.GREEN}{'█' * filled_length}{Fore.LIGHTBLACK_EX}{'░' * (bar_length - filled_length)}"
        print(margin_space + f"{Fore.WHITE}{memory_display} {bar}")
        
        # CPU usage with visual bar
        cpu_display = f"CPU: {Fore.GREEN}{int(cpu_percent)}%"
        
        # Create a visual bar for CPU usage
        cpu_bar_length = bar_length
        cpu_filled_length = int(cpu_bar_length * cpu_percent / 100)
        cpu_bar = f"{Fore.GREEN}{'█' * cpu_filled_length}{Fore.LIGHTBLACK_EX}{'░' * (cpu_bar_length - cpu_filled_length)}"
        print(margin_space + f"{Fore.WHITE}{cpu_display} {cpu_bar}")
        
        # System memory with visual bar
        sys_memory_display = f"System RAM: {Fore.GREEN}{int(system_memory_percent)}%"
        
        # Create a visual bar for system memory usage
        sys_memory_bar_length = bar_length
        sys_memory_filled_length = int(sys_memory_bar_length * system_memory_percent / 100)
        sys_memory_bar = f"{Fore.GREEN}{'█' * sys_memory_filled_length}{Fore.LIGHTBLACK_EX}{'░' * (sys_memory_bar_length - sys_memory_filled_length)}"
        print(margin_space + f"{Fore.WHITE}{sys_memory_display} {sys_memory_bar}")
        
        print()
        
        # Controls Section
        print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}CONTROLS")
        print()
        
        # Game control options
        print(margin_space + f"{Fore.WHITE}[S] {Fore.RED}⏹️  Stop Game and Return to Menu")
        print(margin_space + f"{Fore.WHITE}[M] {Fore.YELLOW}🔙 Return to Menu (Keep Game Running)")
        
        print()
        
        # Game is running message
        print(margin_space + f"{Fore.GREEN}✅ Game is running. Window refreshes automatically.")
        
        while True:
            # Check if the process is still running
            if self.process.poll() is not None:
                # Process has ended - handle game end here
                break
                
            # Check for keypress with timeout
            key = wait_for_key_press(1.0)
            
            if key == 's':
                # Request to stop the game
                print()
                print(margin_space + f"{Fore.RED}{Style.BRIGHT}STOPPING GAME...")
                print()
                
                print(margin_space + f"{Fore.YELLOW}Please wait while Minecraft is closing...")
                
                # Kill the process
                try:
                    self.process.terminate()
                    
                    # Give it 5 seconds to close gracefully
                    for _ in range(5):
                        if self.process.poll() is not None:
                            break
                        time.sleep(1)
                    
                    # Force kill if still running
                    if self.process.poll() is None:
                        print(margin_space + f"{Fore.RED}Minecraft is not responding. Force closing...")
                        self.process.kill()
                except Exception as e:
                    print(f"Error closing Minecraft: {e}")
                
                break
                
            elif key == 'm':
                # Return to menu but keep the game running
                clear_screen()
                print()
                print(margin_space + f"{Fore.YELLOW}Game is still running in the background.")
                print(margin_space + f"{Fore.YELLOW}You can return to the status window from the main menu.")
                time.sleep(2)
                return
                
            # Refresh the display every second
            if not key:  # Only refresh if no key was pressed
                self.display_status()
        
        # Game has ended, show summary
        clear_screen()
        print()
        print(margin_space + f"{Fore.YELLOW}{Style.BRIGHT}GAME ENDED")
        print()
        
        print(margin_space + f"{Fore.GREEN}Minecraft has ended. Session summary:")
        print(margin_space + f"{Fore.WHITE}• Version: {Fore.GREEN}{self.version_id}")
        print(margin_space + f"{Fore.WHITE}• Player: {Fore.GREEN}{self.username}")
        print(margin_space + f"{Fore.WHITE}• Total playtime: {Fore.GREEN}{runtime_str}")
        
        print(margin_space + f"{Fore.CYAN}Press any key to return to main menu...")
        press_any_key_to_continue("", terminal_width=terminal_width)
        
        return

def press_any_key_to_continue(prompt_message="Press any key to continue...", terminal_width=None):
    """Display a prompt and wait for any key press"""
    if terminal_width:
        padding = (terminal_width - len(prompt_message.replace(Fore.CYAN, "").replace(Style.BRIGHT, ""))) // 2
        print(" " * padding + f"{Fore.CYAN}{Style.BRIGHT}{prompt_message}", end="")
    else:
        print(f"{Fore.CYAN}{Style.BRIGHT}{prompt_message}", end="")
    sys.stdout.flush()
    while True:
        key = wait_for_key_press(0.1)
        if key is not None:
            break
    print()  # Add newline after key press

def download_menu(launcher):
    """Display the download menu and handle user input"""
    clear_screen()
    
    # Define box characters for UI
    box_top = f"{Fore.BLUE}{Style.BRIGHT}┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓"
    box_bottom = f"{Fore.BLUE}{Style.BRIGHT}┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
    box_middle = f"{Fore.BLUE}{Style.BRIGHT}┃"
    box_divider = f"{Fore.BLUE}{Style.BRIGHT}┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫"
    
    # Get terminal width for centering
    try:
        terminal_width = os.get_terminal_size().columns
    except:
        terminal_width = 100  # Default if can't determine
    
    menu_width = 60  # Width for centering
    margin = max(0, (terminal_width - menu_width) // 2)
    margin_space = " " * margin
    
    # Get available versions from manifest
    if not os.path.exists(MANIFEST_PATH):
        launcher.get_version_manifest()
    
    with open(MANIFEST_PATH, 'r') as f:
        manifest = json.load(f)
    
    if "versions" not in manifest:
        print(margin_space + f"{Fore.RED}Error: Invalid manifest file!")
        time.sleep(2)
        return

    # Filter for different version types
    releases = []
    snapshots = []
    betas = []
    alphas = []
    
    for v in manifest["versions"]:
        version_id = v["id"]
        version_type = v["type"]
        version_date = v["releaseTime"][:10]  # Just the date part
        
        if version_type == "release":
            releases.append((version_id, version_type, version_date))
        elif version_type == "snapshot":
            snapshots.append((version_id, version_type, version_date))
        elif "beta" in version_id.lower():
            betas.append((version_id, version_type, version_date))
        elif "alpha" in version_id.lower():
            alphas.append((version_id, version_type, version_date))
    
    all_versions = {
        "Release": releases,
        "Snapshot": snapshots,
        "Beta": betas,
        "Alpha": alphas
    }
    
    selected_type = "Release"
    page = 0
    items_per_page = 10
    
    while True:
        clear_screen()
        print()
        print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}MINECRAFT VERSION DOWNLOADER")
        print()
        
        # Version type selector
        print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}VERSION TYPES")
        print()
        
        # Display version type options
        for i, (vtype, versions) in enumerate(all_versions.items()):
            # Highlight the selected type
            if vtype == selected_type:
                print(margin_space + f"{Fore.WHITE}[{i+1}] {Fore.YELLOW}● {vtype} Versions {Fore.CYAN}({len(versions)} available)")
            else:
                print(margin_space + f"{Fore.WHITE}[{i+1}] {Fore.CYAN}{vtype} Versions ({len(versions)} available)")
        
        print()
        
        # Display available versions
        current_list = all_versions[selected_type]
        total_pages = (len(current_list) + items_per_page - 1) // items_per_page
        start_idx = page * items_per_page
        end_idx = min(start_idx + items_per_page, len(current_list))
        
        print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}AVAILABLE VERSIONS {Fore.WHITE}Page {page+1}/{total_pages}")
        print()
        
        if current_list:
            for i in range(start_idx, end_idx):
                version_id, version_type, version_date = current_list[i]
                
                # Color based on version type
                version_color = Fore.GREEN
                if version_type == "snapshot":
                    version_color = Fore.YELLOW
                elif "alpha" in version_id.lower():
                    version_color = Fore.RED
                elif "beta" in version_id.lower():
                    version_color = Fore.MAGENTA
                
                version_display = f"[{i - start_idx + 1}] {version_color}{version_id}{Fore.RESET} ({version_date})"
                print(margin_space + f"{Fore.WHITE}{version_display}")
        else:
            print(margin_space + f"{Fore.RED}No versions available for this type.")
        
        # Pagination controls
        print()
        print(margin_space + f"{Fore.WHITE}[N] {Fore.CYAN}Next Page")
        print(margin_space + f"{Fore.WHITE}[P] {Fore.CYAN}Previous Page")
        print(margin_space + f"{Fore.WHITE}[G] {Fore.CYAN}Go to Page...")
        
        # Add search and back options
        print()
        print(margin_space + f"{Fore.WHITE}[S] {Fore.CYAN}🔍 Search for a specific version")
        print(margin_space + f"{Fore.WHITE}[0] {Fore.RED}🏠 Back to Main Menu")
        print()
        
        # Input section without borders, centered
        print()
        prompt = f"{Fore.CYAN}{Style.BRIGHT}Enter option: "
        padding = (terminal_width - len(prompt.replace(Fore.CYAN, "").replace(Style.BRIGHT, ""))) // 2
        print(" " * padding + prompt, end=f"{Fore.YELLOW}")
        sys.stdout.flush()
        
        # Use immediate input instead of standard input
        choice = get_immediate_input()
        print(choice)  # Show the selection
        
        if choice == "0":
            return
        
        elif choice in ['1', '2', '3', '4']:
            idx = int(choice) - 1
            types = list(all_versions.keys())
            if 0 <= idx < len(types):
                selected_type = types[idx]
                page = 0  # Reset page when changing version type
        
        elif choice == 'n':
            if page < total_pages - 1:
                page += 1
        
        elif choice == 'p':
            if page > 0:
                page -= 1
        
        elif choice == 'g':
            clear_screen()
            print(margin_space + box_top)
            print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════════════╗    {Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}║                       GO TO PAGE                          ║    {Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╚══════════════════════════════════════════════════════════════╝    {Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + box_divider)
            print(margin_space + f"{box_middle} {Fore.WHITE}Total pages: {total_pages}{' ' * 58}{Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + box_bottom)
            
            # Input section without borders, centered
            print()
            prompt = f"{Fore.CYAN}{Style.BRIGHT}Enter page number: "
            padding = (terminal_width - len(prompt.replace(Fore.CYAN, "").replace(Style.BRIGHT, ""))) // 2
            print(" " * padding + prompt, end=f"{Fore.YELLOW}")
            try:
                new_page = int(input().strip()) - 1
                if 0 <= new_page < total_pages:
                    page = new_page
            except ValueError:
                pass
        elif choice == 's':
            clear_screen()
            print(margin_space + box_top)
            print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════════════╗    {Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}║                       VERSION SEARCH                       ║    {Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╚══════════════════════════════════════════════════════════════╝    {Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + box_divider)
            print(margin_space + f"{box_middle} {Fore.WHITE}Enter a search term or specific version ID:{' ' * 32}{Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + box_bottom)
            
            # Input section without borders, centered
            print()
            prompt = f"{Fore.CYAN}{Style.BRIGHT}Search: "
            padding = (terminal_width - len(prompt.replace(Fore.CYAN, "").replace(Style.BRIGHT, ""))) // 2
            print(" " * padding + prompt, end=f"{Fore.YELLOW}")
            search_term = input().strip().lower()
            
            # Search in all versions
            results = []
            for v in manifest["versions"]:
                if search_term in v["id"].lower():
                    results.append((v["id"], v["type"], v["releaseTime"][:10]))
            
            clear_screen()
            print(margin_space + box_top)
            print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════════════╗    {Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}║                       SEARCH RESULTS                         ║    {Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╚══════════════════════════════════════════════════════════════╝    {Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + box_divider)
            
            if results:
                print(margin_space + f"{box_middle} {Fore.WHITE}Found {len(results)} version(s) matching '{search_term}':{' ' * (33 - len(search_term))}{Fore.BLUE}{Style.BRIGHT}┃")
                print(margin_space + box_divider)
                
                for i, (version_id, version_type, version_date) in enumerate(results[:10]):  # Limit to 10 results
                    # Color based on version type
                    version_color = Fore.GREEN
                    if version_type == "snapshot":
                        version_color = Fore.YELLOW
                    elif "alpha" in version_id.lower():
                        version_color = Fore.RED
                    elif "beta" in version_id.lower():
                        version_color = Fore.MAGENTA
                    
                    version_display = f" [{i+1}] {version_color}{version_id}{Fore.RESET} ({version_date})"
                    print(margin_space + f"{box_middle} {Fore.WHITE}{version_display}{' ' * (70 - len(version_display))}{Fore.BLUE}{Style.BRIGHT}┃")
            else:
                print(margin_space + f"{box_middle} {Fore.RED}No results found for '{search_term}'{' ' * (47 - len(search_term))}{Fore.BLUE}{Style.BRIGHT}┃")
            
            print(margin_space + box_divider)
            print(margin_space + f"{box_middle} {Fore.WHITE}Enter a number to download, or 0 to return:{' ' * 30}{Fore.BLUE}{Style.BRIGHT}┃")
            print(margin_space + box_bottom)
            
            # Input section without borders, centered
            print()
            prompt = f"{Fore.CYAN}{Style.BRIGHT}Enter option: "
            padding = (terminal_width - len(prompt.replace(Fore.CYAN, "").replace(Style.BRIGHT, ""))) // 2
            print(" " * padding + prompt, end=f"{Fore.YELLOW}")
            choice = input().strip()
            
            if choice == "0":
                continue
            elif choice.isdigit() and 1 <= int(choice) <= len(results):
                version_id = results[int(choice) - 1][0]
                clear_screen()
                print(margin_space + box_top)
                print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════════════╗    {Fore.BLUE}{Style.BRIGHT}┃")
                print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}║                    DOWNLOADING VERSION                      ║    {Fore.BLUE}{Style.BRIGHT}┃")
                print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╚══════════════════════════════════════════════════════════════╝    {Fore.BLUE}{Style.BRIGHT}┃")
                print(margin_space + box_divider)
                print(margin_space + f"{box_middle} {Fore.YELLOW}Downloading {version_id}...{' ' * (56 - len(version_id))}{Fore.BLUE}{Style.BRIGHT}┃")
                print(margin_space + box_bottom)
                
                launcher.download_version(version_id)
                
                print()
                press_any_key_to_continue("Press any key to continue...", terminal_width)
            # Added option to try downloading a specific version ID directly from search
            elif len(search_term) > 0:
                version_exists = False
                for v in manifest["versions"]:
                    if v["id"].lower() == search_term.lower():
                        version_exists = True
                        choice = v["id"]  # Use the correct case from manifest
                        break
                
                if version_exists:
                    clear_screen()
                    print(margin_space + box_top)
                    print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════════════╗    {Fore.BLUE}{Style.BRIGHT}┃")
                    print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}║                    DOWNLOADING VERSION                      ║    {Fore.BLUE}{Style.BRIGHT}┃")
                    print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╚══════════════════════════════════════════════════════════════╝    {Fore.BLUE}{Style.BRIGHT}┃")
                    print(margin_space + box_divider)
                    print(margin_space + f"{box_middle} {Fore.YELLOW}Downloading {choice}...{' ' * (56 - len(choice))}{Fore.BLUE}{Style.BRIGHT}┃")
                    print(margin_space + box_bottom)
                    
                    launcher.download_version(choice)
                    
                    print()
                    press_any_key_to_continue("Press any key to continue...", terminal_width)
        elif choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= end_idx - start_idx:
                version_id = current_list[start_idx + idx - 1][0]
                clear_screen()
                print(margin_space + box_top)
                print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════════════╗    {Fore.BLUE}{Style.BRIGHT}┃")
                print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}║                    DOWNLOADING VERSION                      ║    {Fore.BLUE}{Style.BRIGHT}┃")
                print(margin_space + f"{box_middle} {Fore.CYAN}{Style.BRIGHT}╚══════════════════════════════════════════════════════════════╝    {Fore.BLUE}{Style.BRIGHT}┃")
                print(margin_space + box_divider)
                print(margin_space + f"{box_middle} {Fore.YELLOW}Downloading {version_id}...{' ' * (56 - len(version_id))}{Fore.BLUE}{Style.BRIGHT}┃")
                print(margin_space + box_bottom)
                
                launcher.download_version(version_id)
                
                print()
                press_any_key_to_continue("Press any key to continue...", terminal_width)

def repair_version_menu(launcher):
    """Display the repair version menu and handle user input"""
    clear_screen()
    
    # Define box characters for UI
    box_top = f"{Fore.BLUE}{Style.BRIGHT}┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓"
    box_bottom = f"{Fore.BLUE}{Style.BRIGHT}┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
    box_middle = f"{Fore.BLUE}{Style.BRIGHT}┃"
    box_divider = f"{Fore.BLUE}{Style.BRIGHT}┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫"
    
    # Get terminal width for centering
    try:
        terminal_width = os.get_terminal_size().columns
    except:
        terminal_width = 100  # Default if can't determine
    
    menu_width = 60  # Width for centering
    margin = max(0, (terminal_width - menu_width) // 2)
    margin_space = " " * margin
    
    print()
    print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}REPAIR VERSION")
    print()
    
    # Get installed versions
    installed_versions = launcher.get_installed_versions()
    
    if not installed_versions:
        print(margin_space + f"{Fore.RED}No installed versions found.")
        print()
        print(margin_space + f"{Fore.CYAN}Press Enter to return to main menu...")
        input()
        return
    
    print(margin_space + f"{Fore.YELLOW}Select a version to repair/re-download:")
    print()
    
    # Display installed versions in a grid format
    version_count = len(installed_versions)
    columns = 2
    rows = (version_count + columns - 1) // columns
    
    for row in range(rows):
        version_row = ""
        for col in range(columns):
            idx = row + col * rows
            if idx < version_count:
                version = installed_versions[idx]
                # Determine version type for color coding
                version_color = Fore.GREEN
                if "snapshot" in version.lower():
                    version_color = Fore.YELLOW
                elif "alpha" in version.lower():
                    version_color = Fore.RED
                elif "beta" in version.lower():
                    version_color = Fore.MAGENTA
                
                # Format the version entry with numbering
                entry = f"[{idx+1}] {version_color}{version}{Fore.RESET}"
                version_row += entry.ljust(35)
        
        print(margin_space + f"{Fore.WHITE}{version_row}")
    
    print()
    print(margin_space + f"{Fore.WHITE}[0] {Fore.RED}🏠 Back to Main Menu")
    print()
    
    # Input section without borders, centered
    print()
    prompt = f"{Fore.CYAN}{Style.BRIGHT}Enter option: "
    padding = (terminal_width - len(prompt.replace(Fore.CYAN, "").replace(Style.BRIGHT, ""))) // 2
    print(" " * padding + prompt, end=f"{Fore.YELLOW}")
    sys.stdout.flush()
    
    # Use immediate input instead of standard input
    choice = get_immediate_input()
    print(choice)  # Show the selection
    
    if choice == "0":
        return
    
    if choice.isdigit() and 1 <= int(choice) <= len(installed_versions):
        version_id = installed_versions[int(choice) - 1]
        
        clear_screen()
        print()
        print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}REPAIR OPTIONS FOR {version_id}")
        print()
        
        print(margin_space + f"{Fore.WHITE}[1] {Fore.YELLOW}🔧 Re-extract native libraries only")
        print(margin_space + f"{Fore.WHITE}[2] {Fore.YELLOW}📦 Completely re-download version")
        print(margin_space + f"{Fore.WHITE}[0] {Fore.RED}🔙 Back")
        print()
        
        # Input section without borders, centered
        print()
        prompt = f"{Fore.CYAN}{Style.BRIGHT}Enter option: "
        padding = (terminal_width - len(prompt.replace(Fore.CYAN, "").replace(Style.BRIGHT, ""))) // 2
        print(" " * padding + prompt, end=f"{Fore.YELLOW}")
        sys.stdout.flush()
        
        # Use immediate input instead of standard input
        repair_choice = get_immediate_input()
        print(repair_choice)  # Show the selection
        
        if repair_choice == "1":
            # Re-extract native libraries
            version_dir = os.path.join(VERSIONS_DIR, version_id)
            json_file = os.path.join(version_dir, f"{version_id}.json")
            
            if os.path.exists(json_file):
                try:
                    clear_screen()
                    print()
                    print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}RE-EXTRACTING LIBRARIES")
                    print()
                    
                    with open(json_file, 'r') as f:
                        version_data = json.load(f)
                    
                    # Clean up old natives directory
                    natives_dir = os.path.join(version_dir, "natives")
                    if os.path.exists(natives_dir):
                        print(margin_space + f"{Fore.YELLOW}Removing old native libraries...")
                        
                        # Try to delete files in the directory, but handle if files are locked
                        for filename in os.listdir(natives_dir):
                            file_path = os.path.join(natives_dir, filename)
                            try:
                                if os.path.isfile(file_path):
                                    os.unlink(file_path)
                            except Exception as e:
                                print(margin_space + f"{Fore.RED}Could not remove {file_path}: {e}")
                    
                    # Extract natives again
                    print(margin_space + f"{Fore.GREEN}Extracting libraries for {version_id}...")
                    launcher.extract_natives(version_data, version_dir)
                    
                    print()
                    print(margin_space + f"{Fore.GREEN}✅ Native libraries have been successfully re-extracted.")
                    
                    print()
                    print(margin_space + f"{Fore.CYAN}Press Enter to continue...")
                    input()
                except Exception as e:
                    print(margin_space + f"{Fore.RED}Error re-extracting native libraries: {e}")
                    
                    print()
                    print(margin_space + f"{Fore.CYAN}Press Enter to continue...")
                    input()
            else:
                print()
                print(margin_space + f"{Fore.RED}Version JSON not found. Cannot repair.")
                
                print()
                print(margin_space + f"{Fore.CYAN}Press Enter to continue...")
                input()
        
        elif repair_choice == "2":
            # Completely re-download version
            version_dir = os.path.join(VERSIONS_DIR, version_id)
            
            clear_screen()
            print()
            print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}CONFIRM RE-DOWNLOAD")
            print()
            
            print(margin_space + f"{Fore.YELLOW}Are you sure you want to completely re-download {version_id}?")
            print(margin_space + f"{Fore.YELLOW}This will delete and re-download all version files.")
            print()
            print(margin_space + f"{Fore.WHITE}[Y] {Fore.GREEN}Yes, re-download")
            print(margin_space + f"{Fore.WHITE}[N] {Fore.RED}No, cancel")
            print()
            
            # Input section without borders, centered
            print()
            prompt = f"{Fore.CYAN}{Style.BRIGHT}Confirm (y/n): "
            padding = (terminal_width - len(prompt.replace(Fore.CYAN, "").replace(Style.BRIGHT, ""))) // 2
            print(" " * padding + prompt, end=f"{Fore.YELLOW}")
            sys.stdout.flush()
            
            # Use immediate input instead of standard input
            confirm = get_immediate_input()
            print(confirm)  # Show the selection
            
            if confirm == "y":
                try:
                    # Delete version directory
                    clear_screen()
                    print()
                    print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}RE-DOWNLOADING VERSION")
                    print()
                    
                    print(margin_space + f"{Fore.YELLOW}Deleting version files...")
                    shutil.rmtree(version_dir)
                    print(margin_space + f"{Fore.GREEN}Version files deleted.")
                    
                    print(margin_space + f"{Fore.YELLOW}Starting download...")
                    
                    launcher.download_version(version_id)
                    
                    clear_screen()
                    print()
                    print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}DOWNLOAD COMPLETE")
                    print()
                    
                    print(margin_space + f"{Fore.GREEN}✅ Version {version_id} has been successfully re-downloaded.")
                    
                    print()
                    print(margin_space + f"{Fore.CYAN}Press Enter to continue...")
                    input()
                except Exception as e:
                    print()
                    print(margin_space + f"{Fore.RED}Error re-downloading version: {e}")
                    
                    print()
                    print(margin_space + f"{Fore.CYAN}Press Enter to continue...")
                    input()

def wait_for_key_press(timeout=0.1):
    """Wait for a key press with timeout and return immediately without requiring Enter"""
    import msvcrt
    import select
    
    # Windows version
    if os.name == 'nt':
        start_time = time.time()
        while time.time() - start_time < timeout:
            if msvcrt.kbhit():
                return msvcrt.getch().decode('utf-8').lower()
            time.sleep(0.01)
    # Unix/Linux/MacOS version
    else:
        import termios
        import tty
        
        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())
            i, o, e = select.select([sys.stdin], [], [], timeout)
            if i:
                return sys.stdin.read(1).lower()
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
    return None

def get_immediate_input():
    """Get a single character input without requiring Enter"""
    if os.name == 'nt':
        import msvcrt
        # Wait for a key press
        while True:
            if msvcrt.kbhit():
                key = msvcrt.getch().decode('utf-8').lower()
                # Return the key immediately
                return key
            time.sleep(0.01)
    else:
        import termios
        import tty
        import select
        
        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())
            # Wait for input without timeout
            while True:
                i, o, e = select.select([sys.stdin], [], [])
                if i:
                    return sys.stdin.read(1).lower()
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

def clear_screen():
    """Clear the terminal screen"""
    os.system('cls' if os.name == 'nt' else 'clear')

def main_menu(launcher):
    """Display the main menu and handle user input"""
    clear_screen()
    
    # Define box characters for UI
    box_top = f"{Fore.BLUE}{Style.BRIGHT}┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓"
    box_bottom = f"{Fore.BLUE}{Style.BRIGHT}┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
    box_middle = f"{Fore.BLUE}{Style.BRIGHT}┃"
    box_divider = f"{Fore.BLUE}{Style.BRIGHT}┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫"
    
    # Get terminal width for centering
    try:
        terminal_width = os.get_terminal_size().columns
    except:
        terminal_width = 100  # Default if can't determine
    
    menu_width = 60  # Width for centering
    margin = max(0, (terminal_width - menu_width) // 2)
    margin_space = " " * margin
    
    print()
    print(margin_space + f"{Fore.GREEN}{Style.BRIGHT}MINECRAFT {Fore.MAGENTA}ECHO LAUNCHER")
    print()
    
    # Continue with the rest of the menu, but without boxes
    installed_versions = launcher.get_installed_versions()
    
    if installed_versions:
        print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}INSTALLED VERSIONS")
        print()
        
        # Display installed versions in a grid format
        version_count = len(installed_versions)
        columns = 2
        rows = (version_count + columns - 1) // columns
        
        for row in range(rows):
            version_row = ""
            for col in range(columns):
                idx = row + col * rows
                if idx < version_count:
                    version = installed_versions[idx]
                    # Determine version type for color coding
                    version_color = Fore.GREEN
                    if "snapshot" in version.lower():
                        version_color = Fore.YELLOW
                    elif "alpha" in version.lower():
                        version_color = Fore.RED
                    elif "beta" in version.lower():
                        version_color = Fore.MAGENTA
                    
                    # Format the version entry with numbering
                    entry = f"[{idx+1}] {version_color}{version}{Fore.RESET}"
                    version_row += entry.ljust(35)
            
            print(margin_space + f"{Fore.WHITE}{version_row}")
        
        print()
    
    # Main menu options
    print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}MAIN MENU")
    print()
    
    # Define menu options with icons
    options = [
        (f"{Fore.YELLOW}📦 Download Version", "D"),
        (f"{Fore.YELLOW}🔧 Repair/Re-download Version", "R"),
        (f"{Fore.YELLOW}⚙️  Settings", "S"),
        (f"{Fore.YELLOW}☕ Java Settings", "J"),
        (f"{Fore.RED}🚪 Quit", "Q")
    ]
    
    # Print menu options
    for i, (option_text, key) in enumerate(options):
        print(margin_space + f"{Fore.WHITE}[{key}] {option_text}")
    
    print()
    
    # System information section
    print(margin_space + f"{Fore.CYAN}{Style.BRIGHT}SYSTEM STATUS")
    print()
    
    # User information
    username_display = f"👤 Current user: {Fore.GREEN}{launcher.config['username']}"
    print(margin_space + f"{Fore.WHITE}{username_display}")
    
    # RAM information with a visual indicator of allocated amount
    ram_value = launcher.config["ram"]
    ram_display = f"🧠 RAM: {Fore.GREEN}{ram_value} GB "
    ram_bar_length = min(int(ram_value * 5), 40)  # Scale the bar with the RAM value
    ram_bar = f"{Fore.GREEN}{'█' * ram_bar_length}{Fore.LIGHTBLACK_EX}{'░' * (40 - ram_bar_length)}"
    print(margin_space + f"{Fore.WHITE}{ram_display}{ram_bar}")
    
    # Java information
    java_display = "☕ Java: "
    if launcher.config["preferred_java_version"]:
        java_display += f"{Fore.GREEN}{launcher.config['preferred_java_version']}"
    elif launcher.config["java_path"]:
        java_display += f"{Fore.GREEN}Custom ({launcher.config['java_path']})"
    else:
        java_display += f"{Fore.YELLOW}Auto-select for each version"
    
    print(margin_space + f"{box_middle} {Fore.WHITE}{java_display}{' ' * (70 - len(java_display))}{Fore.BLUE}{Style.BRIGHT}┃")
    
    print(margin_space + box_bottom)
    
    # Input section without borders, centered
    print()
    prompt = f"{Fore.CYAN}{Style.BRIGHT}Enter option or version number: "
    padding = (terminal_width - len(prompt.replace(Fore.CYAN, "").replace(Style.BRIGHT, ""))) // 2
    print(" " * padding + prompt, end=f"{Fore.YELLOW}")
    sys.stdout.flush()  # Ensure the prompt is displayed
    
    # Use immediate input instead of standard input
    choice = get_immediate_input()
    print(choice)  # Show the selected key
    
    if choice == "q":
        sys.exit(0)
    
    elif choice == "d":
        download_menu(launcher)
    
    elif choice == "r":
        repair_version_menu(launcher)
    
    elif choice == "s":
        launcher.change_settings()
    
    elif choice == "j":
        launcher.java_version_menu()
    
    elif choice.isdigit():
        index = int(choice) - 1
        if 0 <= index < len(installed_versions):
            version_id = installed_versions[index]
            launcher.launch_game(version_id)
        else:
            print(f"{Fore.RED}Invalid selection!")
            time.sleep(1)

def main():
    """Main entry point"""
    launcher = MinecraftLauncher()
    
    while True:
        try:
            main_menu(launcher)
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Exiting...")
            break
        except Exception as e:
            print(f"{Fore.RED}An error occurred: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main() 