#!/usr/bin/env python3
"""
Debug script for the pre-filtering functionality.
"""

import sys
import os
sys.path.append(os.path.dirname(__file__))

from pre_filter_utils import ContentPreFilter
from config import Config


def debug_pre_filter():
    """Debug the pre-filtering functionality."""
    
    # Create a minimal config
    config = Config(
        m3u="",
        sqlite_cache_file=None,
        log_file=None,
        output_dir=None,
        existing_media_dirs=[],
        tmdb_api="test_key",
        allowed_movie_countries=["US"],
        allowed_tv_countries=["US"],
    )
    
    # Initialize pre-filter
    pre_filter = ContentPreFilter(config)
    
    # Test a Japanese title
    title = "Naruto Shippuden"
    print(f"Original title: '{title}'")
    
    # Test normalization
    normalized = pre_filter._normalize_title(title)
    print(f"Normalized title: '{normalized}'")
    
    # Test each pattern
    print("\nTesting patterns:")
    for i, pattern in enumerate(pre_filter.non_us_patterns):
        if pattern['regex'].search(normalized):
            print(f"  ✓ Pattern {i+1} ({pattern['name']}): MATCH")
        else:
            print(f"  ✗ Pattern {i+1} ({pattern['name']}): NO MATCH")
    
    # Test language indicators
    print(f"\nTesting language indicators for '{normalized.lower()}':")
    for language, indicators in pre_filter.language_indicators.items():
        matches = [indicator for indicator in indicators if indicator in normalized.lower()]
        if matches:
            print(f"  ✓ {language}: {matches}")
    
    # Test the main function
    result, reason, confidence = pre_filter.should_skip_tmdb(title)
    print(f"\nResult: {result}, Reason: {reason}, Confidence: {confidence}")


if __name__ == "__main__":
    debug_pre_filter()
