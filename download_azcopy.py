import os
import sys
import zipfile
import urllib.request
import tempfile
import shutil

AZCOPY_URL = "https://aka.ms/downloadazcopy-v10-windows"
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_PATH = os.path.join(WORKSPACE_DIR, "azcopy.exe")

def download_and_extract_azcopy():
    if os.path.exists(TARGET_PATH):
        print(f"AzCopy already exists at {TARGET_PATH}")
        return True

    print(f"Downloading AzCopy from {AZCOPY_URL}...")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "azcopy.zip")
            # Download the zip file
            urllib.request.urlretrieve(AZCOPY_URL, zip_path)
            print("Download complete. Extracting zip archive...")
            
            # Extract zip
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
                
            # Locate azcopy.exe inside extracted files
            extracted_bin = None
            for root, dirs, files in os.walk(temp_dir):
                if "azcopy.exe" in files:
                    extracted_bin = os.path.join(root, "azcopy.exe")
                    break
                    
            if extracted_bin:
                shutil.copy(extracted_bin, TARGET_PATH)
                print(f"Successfully placed azcopy.exe at {TARGET_PATH}")
                return True
            else:
                print("Error: Could not find azcopy.exe in the downloaded archive.")
                return False
    except Exception as e:
        print(f"Failed to download/extract AzCopy: {e}")
        return False

if __name__ == "__main__":
    success = download_and_extract_azcopy()
    sys.exit(0 if success else 1)
