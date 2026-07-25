"""GPG-based encryption utilities for files and tarballs."""

import subprocess
import os

def encrypt_file_gpg(input_path, output_path, passphrase_env):
    """
    Encrypt a file using GPG symmetric encryption.
    :param input_path: Path to the file to encrypt.
    :param output_path: Path to write the encrypted file.
    :param passphrase_env: Name of the environment variable holding the passphrase.
    :raises: RuntimeError if encryption fails.
    """
    # Retrieve the passphrase from the specified environment variable
    passphrase = os.environ.get(passphrase_env)
    if not passphrase:
        raise RuntimeError(f"GPG passphrase environment variable '{passphrase_env}' not set.")

    # Build the GPG command for symmetric encryption using AES256
    cmd = [
        "gpg",
        "--batch",          # Run in batch mode (no interactive input)
        "--yes",            # Overwrite output file if it exists
        "--symmetric",      # Use symmetric encryption
        "--cipher-algo", "AES256",  # Specify the cipher algorithm
        "--passphrase", passphrase, # Provide the passphrase directly
        "-o", output_path,          # Output file
        input_path                 # Input file
    ]
    # Execute the GPG command
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        # Raise an error if encryption failed, including stderr output
        raise RuntimeError(f"GPG encryption failed: {result.stderr.decode()}")

def encrypt_tarballs(tarball_paths, config, logger):
    """
    Encrypts each tarball in tarball_paths using GPG and removes the original.
    Returns a list of encrypted tarball paths.
    :param tarball_paths: List of tarball file paths to encrypt.
    :param config: Job configuration dictionary (should include encryption settings).
    :param logger: Logger instance for logging messages.
    :return: List of encrypted tarball file paths.
    """

    # Determine which environment variable holds the passphrase
    passphrase_env = (
        config.get("encryption", {}).get("passphrase_env")
        or "JABS_ENCRYPT_PASSPHRASE"
    )
    encrypted_paths = []
    # Iterate over each tarball and encrypt it
    for tarball_path in tarball_paths:
        encrypted_path = tarball_path + ".gpg"
        try:
            # Encrypt the tarball using GPG
            encrypt_file_gpg(tarball_path, encrypted_path, passphrase_env)
            # Remove the original unencrypted tarball
            os.remove(tarball_path)
            logger.info(f"Encrypted and removed: {tarball_path}")
            encrypted_paths.append(encrypted_path)
        except (RuntimeError, OSError) as exc:
            # Log any errors encountered during encryption
            logger.error(f"Failed to encrypt {tarball_path}: {exc}", exc_info=True)
            # Optionally: raise or continue to next file
    return encrypted_paths
