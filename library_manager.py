import os
import json
import requests
import platform
import threading
import queue
import concurrent.futures
from tqdm import tqdm
from colorama import Fore, Style

class LibraryManager:
    def __init__(self, libraries_dir):
        self.libraries_dir = libraries_dir
        self.natives_dir = "natives"
        self.max_workers = min(32, os.cpu_count() * 4)  # Balance performance with resource usage
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Echo-Launcher/1.0',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive'
        })
        
        # Create directories if they don't exist
        if not os.path.exists(self.libraries_dir):
            os.makedirs(self.libraries_dir)
        
        if not os.path.exists(self.natives_dir):
            os.makedirs(self.natives_dir)
    
    def download_libraries(self, version_data):
        """Download libraries for the specified version using multithreading"""
        if "libraries" not in version_data:
            print(f"{Fore.RED}No libraries found in version data!")
            return False
        
        libraries = version_data["libraries"]
        total_libraries = len(libraries)
        current_os = get_os_name()
        
        print(f"{Fore.YELLOW}Downloading libraries ({total_libraries} libraries) using {self.max_workers} threads...")
        
        # Queue for tracking results
        results_queue = queue.Queue()
        
        # Setup progress bar
        pbar = tqdm(
            total=total_libraries,
            desc="Libraries",
            unit="library",
            bar_format="{l_bar}%s{bar}%s{r_bar}" % (Fore.GREEN, Fore.RESET)
        )
        
        # Create a list of download tasks
        download_tasks = []
        
        for lib_entry in libraries:
            # Skip libraries that are not for the current OS
            if not self._should_download_library(lib_entry, current_os):
                pbar.update(1)
                continue
            
            # Process main library JAR
            if "downloads" in lib_entry and "artifact" in lib_entry["downloads"]:
                artifact = lib_entry["downloads"]["artifact"]
                path = artifact["path"] if "path" in artifact else self._make_path_from_name(lib_entry["name"])
                download_tasks.append((
                    artifact["url"],
                    os.path.join(self.libraries_dir, path),
                    results_queue,
                    pbar
                ))
            else:
                pbar.update(1)
            
            # Process natives if present
            if "downloads" in lib_entry and "classifiers" in lib_entry["downloads"]:
                classifiers = lib_entry["downloads"]["classifiers"]
                
                # Get the right native for the current OS
                native_key = None
                if "natives" in lib_entry:
                    natives = lib_entry["natives"]
                    if current_os in natives:
                        native_key = natives[current_os].replace("${arch}", platform.architecture()[0][:2])
                
                if native_key and native_key in classifiers:
                    native = classifiers[native_key]
                    native_path = native["path"] if "path" in native else self._make_path_from_name(lib_entry["name"], native_key)
                    download_tasks.append((
                        native["url"],
                        os.path.join(self.libraries_dir, native_path),
                        results_queue,
                        pbar
                    ))
        
        # Create thread-local sessions for connection pooling
        thread_local = threading.local()
        
        def get_session():
            if not hasattr(thread_local, "session"):
                thread_local.session = requests.Session()
                thread_local.session.headers.update({
                    'User-Agent': 'Echo-Launcher/1.0',
                    'Accept-Encoding': 'gzip, deflate',
                    'Connection': 'keep-alive'
                })
            return thread_local.session
        
        # Download function using thread-local session
        def download_with_session(url, path, results_queue, pbar):
            try:
                # Create directories if they don't exist
                directory = os.path.dirname(path)
                if not os.path.exists(directory):
                    os.makedirs(directory, exist_ok=True)
                
                # Skip if the file already exists
                if os.path.exists(path):
                    results_queue.put("success")
                    return
                
                # Download the file
                session = get_session()
                response = session.get(url)
                response.raise_for_status()
                
                with open(path, 'wb') as f:
                    f.write(response.content)
                
                results_queue.put("success")
            except (requests.RequestException, OSError):
                results_queue.put("failed")
            finally:
                pbar.update(1)
        
        # Process downloads in thread pool with thread-local sessions
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(download_with_session, *task) for task in download_tasks]
            concurrent.futures.wait(futures)
        
        # Get results
        success_count = 0
        failed_count = 0
        
        while not results_queue.empty():
            result = results_queue.get()
            if result == "success":
                success_count += 1
            else:
                failed_count += 1
        
        pbar.close()
        print(f"{Fore.GREEN}Libraries downloaded: {success_count} successful, {failed_count} failed")
        return True
    
    def _download_library(self, url, path, results_queue, pbar):
        """Download a single library file"""
        try:
            # Create directories if they don't exist
            directory = os.path.dirname(path)
            if not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)
            
            # Skip if the file already exists
            if os.path.exists(path):
                results_queue.put("success")
                return
            
            # Download the file
            response = self.session.get(url)
            response.raise_for_status()
            
            with open(path, 'wb') as f:
                f.write(response.content)
            
            results_queue.put("success")
        except (requests.RequestException, OSError):
            results_queue.put("failed")
        finally:
            pbar.update(1)
    
    def _should_download_library(self, library, current_os):
        """Check if the library should be downloaded for the current OS"""
        # Skip if there's a rules section that excludes the current OS
        if "rules" in library:
            allowed = False
            for rule in library["rules"]:
                action = rule.get("action", "allow") == "allow"
                
                # If no OS is specified, this rule applies to all OSes
                if "os" not in rule:
                    allowed = action
                    continue
                
                os_name = rule["os"].get("name")
                if os_name and os_name == current_os:
                    allowed = action
            
            if not allowed:
                return False
        
        return True
    
    def _make_path_from_name(self, name, classifier=None):
        """Convert a library name to a path"""
        parts = name.split(':')
        
        if len(parts) < 3:
            # Invalid format
            return None
        
        group_id, artifact_id, version = parts
        
        group_path = group_id.replace('.', '/')
        
        if classifier:
            filename = f"{artifact_id}-{version}-{classifier}.jar"
        else:
            filename = f"{artifact_id}-{version}.jar"
        
        return f"{group_path}/{artifact_id}/{version}/{filename}"

def get_os_name():
    """Get the current OS name in Minecraft format"""
    system = platform.system().lower()
    
    if system == "windows":
        return "windows"
    elif system == "darwin":
        return "osx"
    else:
        return "linux" 