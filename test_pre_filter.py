#!/usr/bin/env python3
"""
Test script for the pre-filtering functionality.
"""

import sys
import os
sys.path.append(os.path.dirname(__file__))

from pre_filter_utils import ContentPreFilter
from config import Config


def test_pre_filter():
    """Test the pre-filtering functionality with sample titles."""
    
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
    
    # Test cases
    test_cases = [
        # Japanese content
        ("Naruto Shippuden", True, "Japanese characters"),
        ("Attack on Titan", True, "Japanese keywords"),
        ("Spirited Away", True, "Japanese keywords"),
        
        # Korean content
        ("Kingdom", True, "Korean characters"),
        ("Crash Landing on You", True, "Korean content"),
        ("Descendants of the Sun", True, "Korean content"),
        
        # Chinese content
        ("Hero", True, "Chinese characters"),
        ("Crouching Tiger, Hidden Dragon", True, "Chinese content"),
        ("The Monkey King", True, "Chinese content"),
        
        # Indian content
        ("Lagaan", True, "Indian content"),
        ("3 Idiots", True, "Indian content"),
        ("Dangal", True, "Indian content"),
        
        # French content
        ("Amélie", True, "French content"),
        ("Le Fabuleux Destin d'Amélie Poulain", True, "French content"),
        
        # German content
        ("Das Boot", True, "German content"),
        ("Good Bye Lenin!", True, "German content"),
        
        # Spanish content
        ("El Laberinto del Fauno", True, "Spanish content"),
        ("La Casa de Papel", True, "Spanish content"),
        
        # US content (should not be filtered)
        ("The Matrix", False, "US content"),
        ("Breaking Bad", False, "US content"),
        ("Stranger Things", False, "US content"),
        ("Game of Thrones", False, "US content"),
        ("The Office", False, "US content"),
        
        # Mixed cases
        ("The Avengers (2012)", False, "US content with year"),
        ("Inception 1080p", False, "US content with quality"),
        ("The Dark Knight - 2008", False, "US content with year"),
    ]
    
    print("Testing Pre-Filter Functionality")
    print("=" * 50)
    
    passed = 0
    failed = 0
    
    for title, should_skip, description in test_cases:
        result, reason, confidence = pre_filter.should_skip_tmdb(title)
        
        if result == should_skip:
            status = "✓ PASS"
            passed += 1
        else:
            status = "✗ FAIL"
            failed += 1
        
        print(f"{status} | {title:<35} | Expected: {'SKIP' if should_skip else 'TMDB'} | Got: {'SKIP' if result else 'TMDB'} | {description}")
        if result:
            print(f"      | Reason: {reason} | Confidence: {confidence}")
    
    print("\n" + "=" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    
    # Print statistics
    stats = pre_filter.get_stats()
    print(f"\nPre-filter Statistics:")
    print(f"  Total checked: {stats['total_checked']}")
    print(f"  Bypassed by pattern: {stats['bypassed_by_pattern']}")
    print(f"  Bypassed by language: {stats['bypassed_by_language']}")
    print(f"  Sent to TMDb: {stats['sent_to_tmdb']}")
    print(f"  Bypass rate: {stats['bypass_rate_percent']}%")
    
    return failed == 0


if __name__ == "__main__":
    success = test_pre_filter()
    sys.exit(0 if success else 1)
