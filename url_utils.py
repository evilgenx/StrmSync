import logging
import tempfile
from pathlib import Path
from typing import Union
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def download_m3u_from_url(url: str, timeout: int = 30) -> Path:
    """
    Download M3U content from a URL and return a temporary file path.
    
    Args:
        url: The URL to download the M3U file from
        timeout: Request timeout in seconds
        
    Returns:
        Path to temporary file containing the downloaded M3U content
        
    Raises:
        requests.RequestException: If download fails
    """
    # Create a session with retry strategy
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    logging.info(f"Downloading M3U from URL: {url}")
    
    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        
        # Create a temporary file to store the M3U content
        temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.m3u', delete=False, encoding='utf-8')
        temp_file_path = Path(temp_file.name)
        
        # Write the content to the temporary file
        temp_file.write(response.text)
        temp_file.close()
        
        logging.info(f"Successfully downloaded M3U from URL, saved to temporary file: {temp_file_path}")
        return temp_file_path
        
    except requests.RequestException as e:
        logging.error(f"Failed to download M3U from URL {url}: {e}")
        raise


def is_url(source: str) -> bool:
    """
    Check if the given source is a URL.
    
    Args:
        source: The source string to check
        
    Returns:
        bool: True if source is a URL, False otherwise
    """
    return isinstance(source, str) and source.startswith(('http://', 'https://'))


def get_m3u_path(source: Union[str, Path]) -> Path:
    """
    Get a Path object for the M3U source, handling both local files and URLs.
    
    Args:
        source: Either a local file path or a URL
        
    Returns:
        Path object pointing to the M3U file (local file or temporary downloaded file)
        
    Raises:
        ValueError: If source is invalid
        requests.RequestException: If URL download fails
    """
    if isinstance(source, Path):
        return source
    
    if isinstance(source, str):
        if is_url(source):
            return download_m3u_from_url(source)
        else:
            return Path(source)
    
    raise ValueError(f"Invalid M3U source type: {type(source)}")
