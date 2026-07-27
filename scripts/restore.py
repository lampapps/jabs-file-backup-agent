#!/usr/bin/env python3
"""JABS Archive Restore Utility: Stand alone script that decrypts and extracts backup archives."""
#v0.1.0
import os
import glob
import subprocess
import shutil
import getpass
import re
from datetime import datetime

# --- Configuration ---
PASSPHRASE_ENV_VAR = "JABS_ENCRYPT_PASSPHRASE"
UNENCRYPTED_PATTERN = "*.tar.gz"
ENCRYPTED_PATTERN = "*.tar.gz.gpg"

# --- Helper Functions ---

def find_archives(directory="."):
    """Finds .tar.gz and .tar.gz.gpg files in the specified directory."""
    unencrypted_files = glob.glob(os.path.join(directory, UNENCRYPTED_PATTERN))
    encrypted_files = glob.glob(os.path.join(directory, ENCRYPTED_PATTERN))

    all_archives_paths = sorted(list(set(unencrypted_files + encrypted_files)))
    # Return only basenames, sorted alphabetically for consistent display
    return sorted([os.path.basename(f) for f in all_archives_paths])

def display_archives(archives):
    """Displays a numbered list of archives."""
    if not archives:
        print("No archives found in the current directory.")
        return
    print("\nFound archives:")
    for i, archive_name in enumerate(archives):
        print(f"  {i+1}. {archive_name}")

def get_passphrase():
    """Gets the GPG passphrase from environment variable or user input."""
    passphrase = os.environ.get(PASSPHRASE_ENV_VAR)
    if passphrase:
        print(f"Using passphrase from environment variable '{PASSPHRASE_ENV_VAR}'.")
        return passphrase
    else:
        print(f"Environment variable '{PASSPHRASE_ENV_VAR}' not set.")
        while True:
            try:
                p = getpass.getpass("Enter GPG passphrase: ")
                if p:
                    return p
                print("Passphrase cannot be empty.")
            except EOFError: # Handle Ctrl+D
                print("\nOperation cancelled by user.")
                return None
            except KeyboardInterrupt: # Handle Ctrl+C
                print("\nOperation cancelled by user.")
                return None

def check_command_exists(command_name):
    """Checks if a command exists on the system."""
    if not shutil.which(command_name):
        print(
            f"Error: '{command_name}' command not found. "
            "Please ensure it is installed and in your PATH."
        )
        return False
    return True

def extract_timestamp_from_filename(filename):
    """
    Extracts a timestamp (YYYYMMDD-HHMMSS or YYYYMMDD_HHMMSS) from the filename.
    Returns a datetime object or None.
    Example: mybackup_20230101-143000.tar.gz -> datetime(2023, 1, 1, 14, 30, 0)
    """
    # Regex to find YYYYMMDD followed by - or _ then HHMMSS
    # It looks for the timestamp pattern anywhere before common archive extensions
    match = re.search(r'(\d{8})[_|-](\d{6})', filename)
    if match:
        date_str, time_str = match.groups()
        try:
            return datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M%S")
        except ValueError:
            return None
    return None

def decrypt_and_extract_gpg(gpg_file_path, passphrase, destination_dir="."):
    """Decrypts a .gpg file and extracts its contents using a pipe."""
    print(f"\nAttempting to decrypt and extract '{gpg_file_path}'...")
    if not check_command_exists("gpg") or not check_command_exists("tar"):
        return False

    gpg_command = [
        "gpg", "--batch", "--yes", "--decrypt",
        "--passphrase", passphrase,
        gpg_file_path
    ]
    # Create tar command to extract from stdin to destination_dir
    tar_command = [
        "tar", "-xzvf", "-", "-C", destination_dir 
        # -z for gzip, -v for verbose, -f - for stdin
    ]

    try:
        with subprocess.Popen(gpg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as gpg_process:
            with subprocess.Popen(
                tar_command, stdin=gpg_process.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ) as tar_process:

                # Allow gpg_process to send its output to tar_process
                if gpg_process.stdout:
                    gpg_process.stdout.close()

                tar_stdout, tar_stderr = tar_process.communicate()
                # Ensure gpg_process also finishes
                _, gpg_stderr = gpg_process.communicate()


                if gpg_process.returncode != 0:
                    print(f"Error during GPG decryption of '{gpg_file_path}':")
                    print(gpg_stderr.decode(errors='replace').strip())
                    return False
                
                if tar_process.returncode != 0:
                    # tar might return non-zero for benign reasons (e.g. future timestamps)
                    # but still print stderr if it exists
                    print(
                        f"Tar extraction from '{gpg_file_path}' completed with code {tar_process.returncode}."
                    )
                    if tar_stderr:
                        print(
                            "Tar errors/warnings:\n",
                            tar_stderr.decode(errors='replace').strip()
                        )
                    if tar_process.returncode > 1:
                        # Codes 0 and 1 are often acceptable for tar
                        print("Tar extraction may have encountered significant issues.")
                        return False

            print(
                f"Successfully decrypted and extracted '{gpg_file_path}' to '{destination_dir}'."
            )
            if tar_stdout:
                print(
                    "Files extracted (from tar stdout):\n",
                    tar_stdout.decode(errors='replace').strip()
                )
            return True

    except Exception as e:
        print(
            f"An unexpected error occurred during decrypt/extract of '{gpg_file_path}': {e}"
        )
        return False

def extract_tar_gz(tar_file_path, destination_dir="."):
    """Extracts a .tar.gz file."""
    print(f"\nAttempting to extract '{tar_file_path}'...")
    if not check_command_exists("tar"):
        return False

    command = ["tar", "-xzvf", tar_file_path, "-C", destination_dir]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, errors='replace'
        )

        if result.returncode != 0:
            # tar might return non-zero for benign reasons
            print(
                f"Tar extraction of '{tar_file_path}' completed with code {result.returncode}."
            )
            if result.stderr:
                print("Tar errors/warnings:\n", result.stderr.strip())
            if result.returncode > 1:
                # Codes 0 and 1 are often acceptable for tar
                print("Tar extraction may have encountered significant issues.")
                return False

        print(f"Successfully extracted '{tar_file_path}' to '{destination_dir}'.")
        if result.stdout:
            print("Files extracted:\n", result.stdout.strip())
        return True
    except Exception as e:
        print(
            f"An unexpected error occurred during extraction of '{tar_file_path}': {e}"
        )
        return False

def process_archive(archive_name, current_passphrase_holder, prompt_user=True):
    """Processes a single archive (decrypt if needed, then extract)."""
    is_encrypted = archive_name.endswith(".gpg")

    if is_encrypted:
        print(f"\nArchive '{archive_name}' is encrypted.")
        if prompt_user:
            user_choice = input(f"Do you want to decrypt and extract '{archive_name}'? (y/n): ").lower()
            if user_choice != 'y':
                print(f"Skipping '{archive_name}'.")
                return
        if current_passphrase_holder[0] is None:  # Check if passphrase needs to be fetched
            passphrase = get_passphrase()
            if passphrase is None:  # User cancelled passphrase input
                return
            current_passphrase_holder[0] = passphrase  # Store for this session
        decrypt_and_extract_gpg(archive_name, current_passphrase_holder[0])
    else:  # Unencrypted .tar.gz
        print(f"\nArchive '{archive_name}' is not encrypted.")
        if prompt_user:
            user_choice = input(f"Do you want to extract '{archive_name}'? (y/n): ").lower()
            if user_choice != 'y':
                print(f"Skipping '{archive_name}'.")
                return
        extract_tar_gz(archive_name)

# --- Main Script ---
def main():
    print("JABS Archive Restore Utility")
    print("----------------------------")
    print(
        f"This script looks for '{UNENCRYPTED_PATTERN}' and '{ENCRYPTED_PATTERN}' files in the current directory."
    )

    current_passphrase_holder = [None] 

    while True:
        archives = find_archives() # Gets basenames, sorted alphabetically
        if not archives:
            print("No archives found to process. Exiting.")
            break
        display_archives(archives)
        print("\nOptions:")
        print("  Enter a number to process a specific archive.")
        print("  Type 'all' to process all listed archives (oldest first based on filename timestamp).")
        print("  Type 'q' to quit.")
        try:
            choice = input("Your choice: ").strip().lower()
        except EOFError:
            print("\nExiting.")
            break
        except KeyboardInterrupt:
            print("\nExiting.")
            break
        if choice == 'q':
            print("Exiting.")
            break
        if choice == 'all':
            print("\nProcessing all archives (oldest first)...")
            detailed_archives = []
            for name in archives:
                timestamp = extract_timestamp_from_filename(name)
                detailed_archives.append({'name': name, 'timestamp': timestamp})

            # Sort archives: those with timestamps first (oldest to newest),
            # then those without timestamps (alphabetically by name).
            def sort_key(item):
                if item['timestamp'] is None:
                    return (datetime.max, item['name'])
                return (item['timestamp'], item['name'])
            sorted_archives_to_process = sorted(detailed_archives, key=sort_key)
            if not sorted_archives_to_process:
                print("No archives to process after sorting.")
            # Prompt once for all
            confirm = input("Do you want to process all listed archives? (y/n): ").strip().lower()
            if confirm != 'y':
                print("Skipping all archives.")
            else:
                for archive_item in sorted_archives_to_process:
                    archive_name_to_process = archive_item['name']
                    if archive_item['timestamp']:
                        print(f"---> Processing (Timestamp: {archive_item['timestamp']}): {archive_name_to_process}")
                    else:
                        print(f"---> Processing (No Timestamp Parsed): {archive_name_to_process}")
                    process_archive(archive_name_to_process, current_passphrase_holder, prompt_user=False)
                print("\nFinished processing all archives.")
        else:
            try:
                selected_index = int(choice) - 1
                if 0 <= selected_index < len(archives):
                    selected_archive = archives[selected_index] # archives is sorted alphabetically
                    process_archive(selected_archive, current_passphrase_holder)
                else:
                    print("Invalid number. Please try again.")
            except ValueError:
                print("Invalid input. Please enter a number, 'all', or 'q'.")

        print("-" * 30)

if __name__ == "__main__":
    main()
