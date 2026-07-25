"""Utility functions for file collection, exclusion patterns, and tarball creation in JABS backup system."""

import re
import fnmatch
import os
import tarfile
import json
from datetime import datetime

import yaml

from logger import setup_logger

def get_all_files(src, exclude_patterns, logger=None, job_name=None):
    """
    Recursively get all files in src, excluding any that match exclude_patterns.
    Uses the improved should_exclude function to properly handle directory patterns.
    """
    file_list = []
    if logger is None:
        logger = setup_logger(job_name or "backup")
    logger.debug(f"Starting file collection with {len(exclude_patterns)} exclusion patterns")

    # Collect all excluded directories for debugging
    excluded_dirs = []
    excluded_files = []

    for root, dirs, files in os.walk(src):
        # Use explicit indexes for modification during iteration
        i = 0
        while i < len(dirs):
            dir_path = os.path.join(root, dirs[i])

            # Get relative path for logging
            rel_dir = os.path.relpath(dir_path, src)
            logger.debug(f"Checking directory: {rel_dir}")

            # Check if directory should be excluded using the patterns
            if should_exclude(dir_path, exclude_patterns, src, logger=logger):
                #logger.info(f"EXCLUDING directory: {rel_dir}")
                dirs.pop(i)  # Remove from dirs to prevent traversal
                excluded_dirs.append(rel_dir)
            else:
                i += 1

        # Process files
        for file in files:
            file_path = os.path.join(root, file)
            rel_file = os.path.relpath(file_path, src)

            # Check if file should be excluded
            if should_exclude(file_path, exclude_patterns, src, logger=logger):
                #logger.info(f"EXCLUDING file: {rel_file}")
                excluded_files.append(rel_file)
            else:
                file_list.append(file_path)

    logger.debug(f"Excluded directories: {len(excluded_dirs)}")
    logger.debug(f"Excluded files: {len(excluded_files)}")
    logger.debug(f"Including files: {len(file_list)}")

    return file_list

def get_new_or_modified_files(src, manifest_path, exclude_patterns=None):
    """
    Return a list of files that are either new (not present in previous manifest)
    or have a newer mtime than recorded in the manifest.
    """
    exclude_patterns = exclude_patterns or []
    # Load previous manifest file paths and mtimes
    prev_files = {}
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        for entry in manifest.get("files", []):
            # entry should have at least "path" and "mtime"
            prev_files[entry["path"]] = entry.get("mtime", 0)
    # Walk current source tree
    changed_files = []
    for root, filenames in os.walk(src):
        for filename in filenames:
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, src)
            if any(pattern in full_path for pattern in exclude_patterns):
                continue
            try:
                mtime = os.path.getmtime(full_path)
            except (FileNotFoundError, OSError):
                continue
            prev_mtime = prev_files.get(rel_path)
            if prev_mtime is None:
                # New file (not in manifest)
                changed_files.append(full_path)
            elif mtime > prev_mtime:
                # Modified file
                changed_files.append(full_path)
    return changed_files

def create_tar_archives(files, dest_tar_dir, max_tarball_size_mb, logger, backup_type, config):
    """
    Create multiple tar archives from the list of files, each up to max_tarball_size_mb (in MB).
    Returns a list of tarball paths.
    """
    max_tarball_size = max_tarball_size_mb * 1024 * 1024
    tarball_index = 1
    current_tar_size = 0
    tarball_paths = []
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    current_tar_path = os.path.join(
        dest_tar_dir, f"{backup_type}_part_{tarball_index}_{timestamp_str}.tar.gz"
    )
    tar = tarfile.open(current_tar_path, "w:gz")
    tarball_paths.append(current_tar_path)

    # Use the source base for relative paths in the archive
    source_base = config.get("source", "")

    for full_path in files:
        arcname = os.path.relpath(full_path, source_base)
        try:
            # Skip broken symlinks
            if os.path.islink(full_path):
                target = os.readlink(full_path)
                if not os.path.exists(os.path.join(os.path.dirname(full_path), target)):
                    logger.warning(f"Skipping broken symlink: {full_path} -> {target}")
                    continue
            file_size = os.path.getsize(full_path)
        except (FileNotFoundError, OSError) as e:
            logger.warning(f"Skipping file (not found or inaccessible): {full_path} ({e})")
            continue
        if current_tar_size + file_size > max_tarball_size and current_tar_size > 0:
            tar.close()
            logger.debug(f"Tarball created: {current_tar_path} (size: {current_tar_size} bytes)")
            tarball_index += 1
            current_tar_path = os.path.join(
                dest_tar_dir, f"{backup_type}_part_{tarball_index}_{timestamp_str}.tar.gz"
            )
            tar = tarfile.open(current_tar_path, "w:gz")
            tarball_paths.append(current_tar_path)
            current_tar_size = 0
        try:
            tar.add(full_path, arcname=arcname)
            current_tar_size += file_size
        except PermissionError as e:
            logger.warning(f"Skipping file (permission denied): {full_path} ({e})")
        except OSError as e:
            logger.warning(f"Skipping file (OS error): {full_path} ({e})")

    tar.close()
    logger.debug(f"Tarball created: {current_tar_path} (size: {current_tar_size} bytes)")
    return tarball_paths

def find_latest_backup_set(job_dst):
    """
    Find the latest backup set directory in the given job destination.

    Args:
        job_dst (str): Path to the job's backup destination directory.

    Returns:
        str or None: Path to the latest backup set directory, or None if not found.
    """
    # Find the latest backup set directory by timestamp or naming convention
    sets = sorted([
        d for d in os.listdir(job_dst)
        if os.path.isdir(os.path.join(job_dst, d))
    ], reverse=True)
    if sets:
        return os.path.join(job_dst, sets[0])
    return None

def should_exclude(path, exclude_patterns, src=None, logger=None):
    """
    Returns True if the given path should be excluded based on the patterns.
    Correctly handles directory patterns with trailing slashes.
    """
    if not exclude_patterns:
        return False

    # Only log if a logger was provided by the caller (typically the job logger).

    # Get relative path
    rel_path = os.path.relpath(path, src) if src else path
    is_dir = os.path.isdir(path)

    # Normalize path for consistent matching across platforms
    rel_path_norm = rel_path.replace(os.sep, '/')
    if is_dir and not rel_path_norm.endswith('/'):
        rel_path_norm += '/'

    # Exact matching and simple pattern tests
    for pattern in exclude_patterns:
        orig_pattern = pattern

        # Handle directory patterns (with trailing slashes)
        is_dir_pattern = pattern.endswith('/')
        pattern = pattern.rstrip('/')

        # Skip directory patterns for files
        if is_dir_pattern and not is_dir:
            continue

        # Check if this path exactly matches the pattern
        if rel_path_norm.rstrip('/') == pattern:
            # logger.info(f"EXCLUDED: '{rel_path}' exactly matches pattern '{orig_pattern}'")
            return True

        # Check if this path starts with pattern/ (directory prefix match)
        if is_dir_pattern and rel_path_norm.startswith(f"{pattern}/"):
            # logger.info(f"EXCLUDED: '{rel_path}' is in directory '{orig_pattern}'")
            return True

        # Check for basename matches (filename only)
        if fnmatch.fnmatch(os.path.basename(path), pattern):
            # logger.info(f"EXCLUDED: '{rel_path}' basename matches '{orig_pattern}'")
            return True

        # Check for direct glob matches on the whole path
        if fnmatch.fnmatch(rel_path_norm, pattern):
            # logger.info(f"EXCLUDED: '{rel_path}' glob matches '{orig_pattern}'")
            return True

        # Handle ** wildcard patterns for matching any directory level
        if '**' in pattern:
            # Convert ** pattern to a regex
            regex_pattern = pattern.replace('**/', '(.*?/)?')  # Match any directory level
            regex_pattern = regex_pattern.replace('**', '.*?')  # Match any content
            regex_pattern = regex_pattern.replace('*', '[^/]*?')  # Regular glob
            regex_pattern = regex_pattern.replace('?', '.')  # Single character
            regex_pattern = f"^{regex_pattern}$"

            if re.match(regex_pattern, rel_path_norm):
                if logger:
                    logger.info(f"EXCLUDED: '{rel_path}' matches wildcard pattern '{orig_pattern}'")
                return True

        # For directory exclusion patterns, see if any component of the path matches
        if is_dir_pattern:
            path_parts = rel_path_norm.split('/')
            for i, part in enumerate(path_parts):
                # Check if any directory component matches the pattern exactly
                if part == pattern:
                    parent_path = '/'.join(path_parts[:i+1])
                    if logger:
                        logger.info(f"EXCLUDED: '{rel_path}' contains directory component '{pattern}/' at '{parent_path}'")
                    return True

    return False

def extract_tar_info(tar_path, encryption_enabled=False):
    """Extract file metadata from a tarball for DB storage and incremental comparison.

    Called before encryption, so tarballs are always readable here.
    Returns a list of dicts with tarball, path, mtime, size, is_new, is_modified.
    """
    files = []
    tarball_name = os.path.basename(tar_path)
    try:
        with tarfile.open(tar_path, 'r:gz') as tar:
            for member in tar.getmembers():
                if member.isfile():
                    files.append({
                        'tarball': tarball_name,
                        'path': member.name,
                        'mtime': member.mtime,
                        'size': member.size,
                        'is_new': False,
                        'is_modified': False,
                    })
    except Exception as e:
        print(f"Error reading tarball {tar_path}: {e}")
        return []
    return files


def generate_archived_manifest(job_name, backup_set_id, backup_set_path, backup_type,
                                backup_job_id=None, **kwargs):
    """Generate (or regenerate) the single HTML manifest for a backup set.

    Queries ALL files for the entire backup set so incremental/differential runs
    update the same manifest.html rather than creating a new file each time.
    Includes which archive holds each file version.  The output is a fully
    self-contained HTML file — no CDN required.

    Returns the path to the written manifest file, or None on error.
    """
    import json as _json
    from models.backup_files import get_files_for_backup_set_by_job_id
    from models.backup_sets import get_backup_set_by_job_id

    files = get_files_for_backup_set_by_job_id(backup_job_id) if backup_job_id else []
    backup_set_row = get_backup_set_by_job_id(backup_job_id) if backup_job_id else None

    # Always write to the same filename — one manifest per backup set
    manifest_path = os.path.join(backup_set_path, "manifest.html")

    def fmt_size(n):
        for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    total_size = sum(f.get('size', 0) for f in files)
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Build config snapshot section
    config_html = ''
    if backup_set_row and backup_set_row.get('config_snapshot'):
        try:
            cfg = _json.loads(backup_set_row['config_snapshot'])
            config_pretty = _json.dumps(cfg, indent=2)
        except Exception:
            config_pretty = backup_set_row['config_snapshot']
        config_html = (
            '<details class="config-details">'
            '<summary>Job Configuration</summary>'
            f'<pre class="config-pre">{config_pretty}</pre>'
            '</details>'
        )

    rows = []
    for f in files:
        mtime_str = (datetime.fromtimestamp(f['mtime']).strftime('%Y-%m-%d %H:%M:%S')
                     if f.get('mtime') else '')
        raw_size = f.get('size', 0)
        rows.append(
            f'<tr>'
            f'<td>{f["path"]}</td>'
            f'<td class="size-col" data-sort="{raw_size}">{fmt_size(raw_size)}</td>'
            f'<td class="date-col">{mtime_str}</td>'
            f'<td class="archive-col">{f.get("tarball", "")}</td>'
            f'</tr>'
        )

    rows_html = '\n'.join(rows)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>JABS Manifest \u2014 {job_name} / {backup_set_id}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#f5f5f5;color:#333;padding:20px}}
h1{{font-size:1.3em;margin-bottom:14px}}
.meta{{background:#fff;border:1px solid #e0e0e0;border-radius:6px;padding:12px 16px;margin-bottom:10px;display:flex;flex-wrap:wrap;gap:20px;font-size:.9em}}
.meta span{{color:#666}}
.config-details{{background:#fff;border:1px solid #e0e0e0;border-radius:6px;margin-bottom:14px;font-size:.9em}}
.config-details summary{{padding:10px 16px;cursor:pointer;user-select:none;font-weight:600;color:#555}}
.config-details summary:hover{{background:#f9f9f9;border-radius:6px}}
.config-pre{{padding:12px 16px;border-top:1px solid #e0e0e0;font-family:monospace;font-size:12px;white-space:pre-wrap;word-break:break-all;background:#fafafa;border-radius:0 0 6px 6px;line-height:1.5}}
.toolbar{{display:flex;align-items:center;gap:12px;margin-bottom:10px}}
.toolbar input{{padding:7px 11px;border:1px solid #ccc;border-radius:4px;font-size:14px;width:320px}}
.row-count{{font-size:13px;color:#666}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e0e0e0;border-radius:6px;overflow:hidden;font-size:13px}}
thead th{{background:#f0f0f0;padding:8px 12px;text-align:left;border-bottom:2px solid #ddd;cursor:pointer;white-space:nowrap;user-select:none}}
thead th:hover{{background:#e8e8e8}}
thead th.sort-asc::after{{content:' \u25b2';font-size:10px}}
thead th.sort-desc::after{{content:' \u25bc';font-size:10px}}
tbody tr:hover{{background:#f8f8f8}}
tbody td{{padding:6px 12px;border-bottom:1px solid #eee;word-break:break-all}}
.size-col{{text-align:right;white-space:nowrap}}
.date-col{{white-space:nowrap}}
.archive-col{{font-family:monospace;font-size:12px}}
.hidden{{display:none}}
</style>
</head>
<body>
<h1>JABS Backup Manifest</h1>
<div class="meta">
  <div><span>Job: </span><strong>{job_name}</strong></div>
  <div><span>Backup Set: </span><strong>{backup_set_id}</strong></div>
  <div><span>Generated: </span><strong>{generated_at}</strong></div>
  <div><span>Total Files: </span><strong>{len(files)}</strong></div>
  <div><span>Total Size: </span><strong>{fmt_size(total_size)}</strong></div>
</div>
{config_html}
<div class="toolbar">
  <input type="text" id="search" placeholder="Search file paths, archives\u2026" oninput="doFilter()">
  <span class="row-count" id="rowCount"></span>
</div>
<table>
  <thead>
    <tr>
      <th data-col="0">File Path</th>
      <th data-col="1" class="size-col">Size</th>
      <th data-col="2" class="date-col">Modified</th>
      <th data-col="3">Archive</th>
    </tr>
  </thead>
  <tbody id="tbody">
{rows_html}
  </tbody>
</table>
<script>
var allRows=Array.from(document.querySelectorAll('#tbody tr'));
var sortCol=-1,sortAsc=true;
function doFilter(){{
  var q=document.getElementById('search').value.toLowerCase();
  var shown=0;
  allRows.forEach(function(r){{
    var match=!q||r.textContent.toLowerCase().indexOf(q)!==-1;
    r.classList.toggle('hidden',!match);
    if(match)shown++;
  }});
  document.getElementById('rowCount').textContent=
    shown===allRows.length?allRows.length+' files':shown+' of '+allRows.length+' files';
}}
document.querySelectorAll('thead th').forEach(function(th){{
  th.addEventListener('click',function(){{
    var col=parseInt(this.dataset.col);
    if(sortCol===col){{sortAsc=!sortAsc;}}else{{sortCol=col;sortAsc=true;}}
    document.querySelectorAll('thead th').forEach(function(t){{t.classList.remove('sort-asc','sort-desc');}});
    this.classList.add(sortAsc?'sort-asc':'sort-desc');
    var tbody=document.getElementById('tbody');
    allRows.sort(function(a,b){{
      var av=a.cells[col].dataset.sort||a.cells[col].textContent.trim();
      var bv=b.cells[col].dataset.sort||b.cells[col].textContent.trim();
      var isNum=!isNaN(parseFloat(av))&&!isNaN(parseFloat(bv));
      if(isNum)return sortAsc?parseFloat(av)-parseFloat(bv):parseFloat(bv)-parseFloat(av);
      return sortAsc?av.localeCompare(bv):bv.localeCompare(av);
    }});
    allRows.forEach(function(r){{tbody.appendChild(r);}});
    doFilter();
  }});
}});
doFilter();
</script>
</body>
</html>"""

    try:
        with open(manifest_path, 'w', encoding='utf-8') as fh:
            fh.write(html)
        return manifest_path
    except Exception as e:
        print(f"Error writing manifest {manifest_path}: {e}")
        return None


def get_merged_exclude_patterns(config, global_config=None, job_config_path=None, logger=None):
    """
    Get merged exclude patterns from common exclude file and job-specific patterns.

    Args:
        config: Job configuration dictionary
        global_config: Global configuration dictionary (optional)
        job_config_path: Path to the job config file (needed to locate common_exclude.yaml)
        logger: Logger instance for logging

    Returns:
        List of merged exclude patterns
    """
    exclude_patterns = []

    # First check if use_common_exclude is set in job config or inherited from global config
    use_common = config.get("use_common_exclude", False)
    if global_config:
        use_common = config.get("use_common_exclude", global_config.get("use_common_exclude", False))

    # Log the use_common_exclude setting if logger is provided
    if logger:
        #logger.info(f"use_common_exclude setting: {use_common}") debug
        if global_config and "use_common_exclude" in global_config:
            logger.debug(f"Global use_common_exclude setting: {global_config.get('use_common_exclude')}")

    if use_common:
        # Load common_exclude.yaml
        common_exclude_path = os.path.join(os.path.dirname(job_config_path or ""), "..", "common_exclude.yaml")

        try:
            with open(common_exclude_path, "r", encoding="utf-8") as f:
                common_excludes = yaml.safe_load(f)
            if isinstance(common_excludes, dict):
                exclude_patterns.extend(common_excludes.get("exclude", []))
            elif isinstance(common_excludes, list):
                exclude_patterns.extend(common_excludes)
            if logger:
                logger.debug(f"Loaded {len(exclude_patterns)} common exclude patterns")
        except Exception as e:
            if logger:
                logger.warning(f"Could not load common_exclude.yaml: {e}")

    # Add job-specific excludes
    job_excludes = config.get("exclude", [])
    exclude_patterns.extend(job_excludes)
    if logger:
        logger.debug(f"Added {len(job_excludes)} job-specific exclude patterns")

    # Also add any legacy 'exclude_patterns' key. debug - is this needed?
    legacy_excludes = config.get("exclude_patterns", [])
    exclude_patterns.extend(legacy_excludes)
    if legacy_excludes and logger:
        logger.debug(f"Added {len(legacy_excludes)} legacy exclude patterns")

    return exclude_patterns

