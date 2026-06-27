import re
import sys

# Define the regex string that will be passed to AzCopy
AZCOPY_EXCLUDE_REGEX_STR = r".*/$;.*\\.*"

# The 13 actual keys containing backslashes from Phase 0 database
BACKSLASH_KEYS = [
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_1.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_10.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_100.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_11.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_12.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_13.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_14.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_15.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_16.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_17.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_18.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_19.jpg",
    r"ArtOfLivingmatrimony/CustomerData/C:\Users\sas\Downloads\female_profiles\female_profiles\girl_2.jpg"
]

# Examples of folder placeholders (0-byte ending in /)
FOLDER_PLACEHOLDERS = [
    "ArtOfLivingmatrimony/CustomerData/1885/",
    "folder/",
    "nested/path/to/virtual/directory/",
    "empty_folder/"
]

# Examples of valid keys that should NOT be excluded
VALID_KEYS = [
    "ArtOfLivingmatrimony/CustomerData/1885/7eae89f7421a901b90cf0e15c9bcfc4d_f22fadc92ea466072618add638b4ed9a.webp",
    "AjeetKumarMauryaResumeOnline.pdf",
    "Arpit resume Updated.pdf",
    "ArtOfLivingmatrimony/CustomerData/1/084c690cb1731f71fd54a76f3b422663_64f08c70-9c47-4b2d-96cf-fc09b2c6d67b.png"
]

def run_test():
    # Split by semicolon as AzCopy does
    patterns = [re.compile(p) for p in AZCOPY_EXCLUDE_REGEX_STR.split(';')]
    
    print("=" * 60)
    print(" RUNNING EXCLUDE-REGEX VALIDATION TEST")
    print("=" * 60)
    
    failures = 0
    
    # 1. Test backslash keys (should all match and be excluded)
    print("\n[*] Testing Backslash Keys (Expected: EXCLUDE/MATCH)")
    for key in BACKSLASH_KEYS:
        matched = any(p.match(key) for p in patterns)
        status = "PASS" if matched else "FAIL"
        print(f"  [{status}] {key}")
        if not matched:
            failures += 1
            
    # 2. Test folder placeholders (should all match and be excluded)
    print("\n[*] Testing Folder Placeholders (Expected: EXCLUDE/MATCH)")
    for key in FOLDER_PLACEHOLDERS:
        matched = any(p.match(key) for p in patterns)
        status = "PASS" if matched else "FAIL"
        print(f"  [{status}] {key}")
        if not matched:
            failures += 1
            
    # 3. Test valid keys (should NOT match and NOT be excluded)
    print("\n[*] Testing Valid Keys (Expected: KEEP/NO MATCH)")
    for key in VALID_KEYS:
        matched = any(p.match(key) for p in patterns)
        status = "PASS" if not matched else "FAIL"
        print(f"  [{status}] {key}")
        if matched:
            failures += 1
            
    print("\n" + "=" * 60)
    if failures == 0:
        print(" VERDICT: ALL TESTS PASSED! Exclude regex behaves exactly as specified.")
        return True
    else:
        print(f" VERDICT: TEST FAILED! Found {failures} validation errors.")
        return False

if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
