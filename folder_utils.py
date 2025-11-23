import logging
import shutil
import time
from pathlib import Path
from typing import List, Tuple, Dict, Set, Optional


def find_duplicate_folders(
    output_dir: Path, 
    compare_dir: Path, 
    category: str = "Movies"
) -> List[Tuple[Path, Path]]:
    """
    Find duplicate folders between output directory and comparison directory.
    Only compares top-level folders (one level deep) to match against downloaded media archives.
    
    Args:
        output_dir: The output directory (where .strm files are generated)
        compare_dir: The directory to compare against
        category: Either "Movies" or "TV Shows"
    
    Returns:
        List of tuples (output_folder_path, comparison_folder_path) for duplicate folders
    """
    start_time = time.time()
    
    if not compare_dir.exists():
        logging.warning(f"Comparison directory does not exist: {compare_dir}")
        return []
    
    if not output_dir.exists():
        logging.warning(f"Output directory does not exist: {output_dir}")
        return []
    
    # Define the category subdirectory paths
    # For output directory, we expect the category subdirectory (e.g., output_dir/Movies)
    output_category_dir = output_dir / category
    
    # For comparison directory, it might be the category directory itself or contain the category subdirectory
    if (compare_dir / category).exists():
        compare_category_dir = compare_dir / category
    elif compare_dir.exists() and compare_dir.is_dir():
        # The compare_dir might already be the category directory
        compare_category_dir = compare_dir
    else:
        logging.info(f"No {category} directory found in comparison: {compare_dir}")
        return []
    
    if not output_category_dir.exists():
        logging.info(f"No {category} directory found in output: {output_category_dir}")
        return []
    
    duplicates = []
    
    logging.info(f"Scanning comparison directory (one level deep): {compare_category_dir}")
    
    # Find only top-level folders in comparison directory (one level deep)
    compare_folders = set()
    compare_folder_paths = [path for path in compare_category_dir.iterdir() if path.is_dir()]
    total_compare_folders = len(compare_folder_paths)
    
    logging.info(f"Found {total_compare_folders} top-level folders in comparison directory")
    
    processed = 0
    for compare_folder_path in compare_folder_paths:
        if compare_folder_path.is_dir():
            # Get only the folder name (not relative path) for comparison
            folder_name = compare_folder_path.name
            compare_folders.add(folder_name)
            processed += 1
            if processed % 100 == 0:  # Log progress every 100 folders
                logging.info(f"Processed {processed}/{total_compare_folders} comparison folders...")
    
    logging.info(f"Completed scanning comparison directory. Processing output directory...")
    
    # Check for matching folders in output directory (one level deep)
    output_folder_paths = [path for path in output_category_dir.iterdir() if path.is_dir()]
    total_output_folders = len(output_folder_paths)
    
    logging.info(f"Found {total_output_folders} top-level folders in output directory")
    
    processed = 0
    for output_folder_path in output_folder_paths:
        if output_folder_path.is_dir():
            # Get only the folder name (not relative path) for comparison
            folder_name = output_folder_path.name
            
            # Check if this folder name exists in comparison directory
            if folder_name in compare_folders:
                comparison_folder_path = compare_category_dir / folder_name
                duplicates.append((output_folder_path, comparison_folder_path))
                logging.debug(f"Found duplicate folder: {folder_name}")
            
            processed += 1
            if processed % 100 == 0:  # Log progress every 100 folders
                percentage = (processed / total_output_folders) * 100
                logging.info(f"Processed {processed}/{total_output_folders} output folders ({percentage:.1f}%)...")
    
    end_time = time.time()
    duration = end_time - start_time
    
    logging.info(f"Comparison completed in {duration:.2f} seconds. Found {len(duplicates)} duplicate folders.")
    
    return duplicates


def delete_duplicate_folders(
    duplicates: List[Tuple[Path, Path]], 
    dry_run: bool = False,
    require_confirmation: bool = True
) -> Tuple[int, int]:
    """
    Delete duplicate folders from the output directory.
    
    Args:
        duplicates: List of duplicate folder tuples (output_path, comparison_path)
        dry_run: If True, only log what would be deleted without actually deleting
        require_confirmation: If True, prompt user for confirmation before deletion
    
    Returns:
        Tuple of (folders_deleted, files_deleted) counts
    """
    if not duplicates:
        logging.info("No duplicate folders found to delete.")
        return 0, 0
    
    # Log what we found
    logging.info(f"Found {len(duplicates)} duplicate folders:")
    for output_path, comparison_path in duplicates:
        logging.info(f"  - {output_path.relative_to(output_path.parents[2])}")
    
    # Ask for confirmation if required
    if require_confirmation and not dry_run:
        response = input(f"\nDelete {len(duplicates)} duplicate folders from output directory? (y/N): ")
        if response.lower() not in ['y', 'yes']:
            logging.info("Deletion cancelled by user.")
            return 0, 0
    
    folders_deleted = 0
    files_deleted = 0
    
    for output_path, comparison_path in duplicates:
        if dry_run:
            logging.info(f"DRY RUN: Would delete {output_path}")
            continue
        
        try:
            # Count files in the folder before deletion
            file_count = sum(1 for _ in output_path.rglob('*') if _.is_file())
            
            # Delete the folder and all its contents
            shutil.rmtree(output_path)
            
            folders_deleted += 1
            files_deleted += file_count
            
            logging.info(f"Deleted duplicate folder: {output_path} ({file_count} files)")
            
        except Exception as e:
            logging.error(f"Failed to delete {output_path}: {e}")
    
    return folders_deleted, files_deleted


def compare_and_clean_folders(
    output_dir: Path,
    compare_movies_dir: Optional[Path] = None,
    compare_tv_dir: Optional[Path] = None,
    dry_run: bool = False,
    require_confirmation: bool = True
) -> Dict[str, Tuple[int, int]]:
    """
    Main function to compare folders and clean duplicates.
    
    Args:
        output_dir: Output directory containing generated .strm files
        compare_movies_dir: Directory to compare movies against (optional)
        compare_tv_dir: Directory to compare TV shows against (optional)
        dry_run: If True, only log what would be done without making changes
        require_confirmation: If True, prompt user for confirmation before deletion
    
    Returns:
        Dictionary with category as key and tuple (folders_deleted, files_deleted) as value
    """
    results = {}
    
    if dry_run:
        logging.info("=== DRY RUN MODE: No folders will be deleted ===")
    
    # Compare movies if configured
    if compare_movies_dir:
        logging.info(f"Comparing Movies: {output_dir} vs {compare_movies_dir}")
        movie_duplicates = find_duplicate_folders(output_dir, compare_movies_dir, "Movies")
        if movie_duplicates:
            folders_deleted, files_deleted = delete_duplicate_folders(
                movie_duplicates, dry_run, require_confirmation
            )
            results["Movies"] = (folders_deleted, files_deleted)
        else:
            logging.info("No duplicate movie folders found.")
            results["Movies"] = (0, 0)
    
    # Compare TV shows if configured
    if compare_tv_dir:
        logging.info(f"Comparing TV Shows: {output_dir} vs {compare_tv_dir}")
        tv_duplicates = find_duplicate_folders(output_dir, compare_tv_dir, "TV Shows")
        if tv_duplicates:
            folders_deleted, files_deleted = delete_duplicate_folders(
                tv_duplicates, dry_run, require_confirmation
            )
            results["TV Shows"] = (folders_deleted, files_deleted)
        else:
            logging.info("No duplicate TV show folders found.")
            results["TV Shows"] = (0, 0)
    
    # Summary
    total_folders = sum(folders for folders, _ in results.values())
    total_files = sum(files for _, files in results.values())
    
    if dry_run:
        logging.info(f"=== DRY RUN COMPLETE: Would delete {total_folders} folders and {total_files} files ===")
    else:
        logging.info(f"=== CLEANUP COMPLETE: Deleted {total_folders} folders and {total_files} files ===")
    
    return results


def generate_comparison_report(
    output_dir: Path,
    compare_movies_dir: Optional[Path] = None,
    compare_tv_dir: Optional[Path] = None
) -> str:
    """
    Generate a report of duplicate folders without deleting anything.
    
    Args:
        output_dir: Output directory containing generated .strm files
        compare_movies_dir: Directory to compare movies against (optional)
        compare_tv_dir: Directory to compare TV shows against (optional)
    
    Returns:
        Formatted report string
    """
    report_lines = ["=== Folder Comparison Report ==="]
    
    if compare_movies_dir:
        movie_duplicates = find_duplicate_folders(output_dir, compare_movies_dir, "Movies")
        report_lines.append(f"\nMovies: Found {len(movie_duplicates)} duplicate folders")
        for output_path, _ in movie_duplicates:
            report_lines.append(f"  - {output_path.relative_to(output_dir)}")
    
    if compare_tv_dir:
        tv_duplicates = find_duplicate_folders(output_dir, compare_tv_dir, "TV Shows")
        report_lines.append(f"\nTV Shows: Found {len(tv_duplicates)} duplicate folders")
        for output_path, _ in tv_duplicates:
            report_lines.append(f"  - {output_path.relative_to(output_dir)}")
    
    total_duplicates = sum([
        len(find_duplicate_folders(output_dir, compare_movies_dir, "Movies")) if compare_movies_dir else 0,
        len(find_duplicate_folders(output_dir, compare_tv_dir, "TV Shows")) if compare_tv_dir else 0
    ])
    
    report_lines.append(f"\nTotal duplicates found: {total_duplicates}")
    report_lines.append("=== End of Report ===")
    
    return "\n".join(report_lines)
