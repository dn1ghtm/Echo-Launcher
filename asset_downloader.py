import os
import json
import hashlib
import requests
import threading
import queue
import concurrent.futures
from tqdm import tqdm
from colorama import Fore, Style

class AssetDownloader:
    def __init__(self, assets_dir):
        self.assets_dir = assets_dir
        self.objects_dir = os.path.join(assets_dir, "objects")
        self.indexes_dir = os.path.join(assets_dir, "indexes")
        self.max_workers = min(32, os.cpu_count() * 4)  # Balance performance with resource usage
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Echo-Launcher/1.0',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive'
        })
        
        # Create directories if they don't exist
        for directory in [self.objects_dir, self.indexes_dir]:
            if not os.path.exists(directory):
                os.makedirs(directory)
    
    def download_asset_index(self, version_data):
        """Download the asset index file for the given version"""
        if "assetIndex" not in version_data:
            print(f"{Fore.RED}No asset index found in version data!")
            return False
        
        asset_index = version_data["assetIndex"]
        asset_index_id = asset_index["id"]
        asset_index_url = asset_index["url"]
        asset_index_path = os.path.join(self.indexes_dir, f"{asset_index_id}.json")
        
        # Download the asset index file if it doesn't exist
        if not os.path.exists(asset_index_path):
            try:
                print(f"{Fore.YELLOW}Downloading asset index {asset_index_id}...")
                response = self.session.get(asset_index_url)
                response.raise_for_status()
                
                with open(asset_index_path, 'w') as f:
                    json.dump(response.json(), f, indent=4)
                
                print(f"{Fore.GREEN}Asset index downloaded successfully!")
            except requests.RequestException as e:
                print(f"{Fore.RED}Error downloading asset index: {e}")
                return False
        
        return asset_index_id
    
    def download_assets(self, asset_index_id):
        """Download assets based on the given asset index using multithreading"""
        asset_index_path = os.path.join(self.indexes_dir, f"{asset_index_id}.json")
        
        if not os.path.exists(asset_index_path):
            print(f"{Fore.RED}Asset index file not found: {asset_index_path}")
            return False
        
        try:
            with open(asset_index_path, 'r') as f:
                asset_index = json.load(f)
            
            if "objects" not in asset_index:
                print(f"{Fore.RED}Invalid asset index format!")
                return False
            
            objects = asset_index["objects"]
            total_objects = len(objects)
            
            print(f"{Fore.YELLOW}Downloading assets ({total_objects} files) using {self.max_workers} threads...")
            
            # Queue for tracking results
            results_queue = queue.Queue()
            
            # Setup progress bar with multiprocessing-safe counter
            pbar = tqdm(
                total=total_objects,
                desc="Assets",
                unit="file",
                bar_format="{l_bar}%s{bar}%s{r_bar}" % (Fore.GREEN, Fore.RESET)
            )
            
            # Create a list of download tasks
            download_tasks = []
            for asset_path, asset_info in objects.items():
                hash_value = asset_info["hash"]
                hash_prefix = hash_value[:2]
                object_path = os.path.join(self.objects_dir, hash_prefix, hash_value)
                
                # Skip if the file already exists and has the correct hash
                if os.path.exists(object_path) and self._verify_hash(object_path, hash_value):
                    results_queue.put(("success", None))
                    pbar.update(1)
                    continue
                
                # Create directory if it doesn't exist
                object_dir = os.path.dirname(object_path)
                if not os.path.exists(object_dir):
                    os.makedirs(object_dir, exist_ok=True)
                
                # Add task to list
                url = f"https://resources.download.minecraft.net/{hash_prefix}/{hash_value}"
                download_tasks.append((url, object_path, hash_value, results_queue, pbar))
            
            # Create thread-local sessions
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
            
            # Modified download function to use the thread-local session
            def download_with_session(url, object_path, expected_hash, results_queue, pbar):
                try:
                    session = get_session()
                    response = session.get(url)
                    response.raise_for_status()
                    
                    with open(object_path, 'wb') as f:
                        f.write(response.content)
                    
                    # Verify the hash
                    if self._verify_hash(object_path, expected_hash):
                        results_queue.put(("success", None))
                    else:
                        # If hash verification fails, remove the file and count as failed
                        os.remove(object_path)
                        results_queue.put(("failed", None))
                except (requests.RequestException, OSError):
                    results_queue.put(("failed", None))
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
                result, _ = results_queue.get()
                if result == "success":
                    success_count += 1
                else:
                    failed_count += 1
            
            pbar.close()
            print(f"{Fore.GREEN}Assets downloaded: {success_count} successful, {failed_count} failed")
            return True
            
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"{Fore.RED}Error processing asset index: {e}")
            return False
    
    def _download_asset(self, url, object_path, expected_hash, results_queue, pbar):
        """Download a single asset file"""
        try:
            # Download the asset file
            response = self.session.get(url)
            response.raise_for_status()
            
            with open(object_path, 'wb') as f:
                f.write(response.content)
            
            # Verify the hash
            if self._verify_hash(object_path, expected_hash):
                results_queue.put(("success", None))
            else:
                # If hash verification fails, remove the file and count as failed
                os.remove(object_path)
                results_queue.put(("failed", None))
        except (requests.RequestException, OSError):
            results_queue.put(("failed", None))
        finally:
            pbar.update(1)
    
    def _verify_hash(self, file_path, expected_hash):
        """Verify the SHA-1 hash of a file"""
        sha1_hash = hashlib.sha1()
        try:
            with open(file_path, 'rb') as f:
                # Read in chunks to avoid loading large files into memory
                for chunk in iter(lambda: f.read(4096), b''):
                    sha1_hash.update(chunk)
            
            return sha1_hash.hexdigest() == expected_hash
        except:
            return False 