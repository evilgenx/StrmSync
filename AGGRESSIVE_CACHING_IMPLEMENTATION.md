# Aggressive Caching Implementation

## Overview

This document explains the aggressive caching system implemented to dramatically speed up VOD filtering in your M3U2strm_jf application.

## Problem Solved

Your program was making **144,000+ API calls** for 48,131 VOD entries (3 calls per entry on average), causing processing times of **40-54 hours** or more due to rate limiting.

## Solution: Aggressive Caching

### 1. Enhanced SQLite Cache Schema

**New Database Tables:**
- `search_cache` - Caches search results
- `details_cache` - Caches detailed responses

**Cache Key Generation:**
- Uses MD5 hash of media_type + title + year for precise lookups
- Ensures cache hits for identical search queries

### 2. Cache TTL Configuration

**Config Setting:**
```ini
cache_ttl_days = 7
```

**Purpose:**
- Content metadata rarely changes, so 7-day cache is safe
- Automatically expires old cache entries
- Configurable for different needs

### 3. Smart Cache Lookup Logic

**Before Every API Call:**
1. Check cache for existing response
2. If cache hit and not expired → use cached data
3. If cache miss or expired → make API call and cache result

**Cache Layers:**
- **Search Results**: Movie/TV search queries
- **Details Results**: Release dates, network info, production countries

### 4. Performance Impact

**Expected Results:**
- **Cache Hit Rate**: 70-75% for duplicate titles
- **API Calls Reduced**: From 144,393 to ~30,000-40,000
- **Processing Time**: From 40-54 hours down to **8-12 hours**
- **Improvement**: **70-80% faster** processing

### 5. Implementation Details

**Files Modified:**
- `core.py` - Added SQLiteCache methods for caching
- `m3u_utils.py` - Integrated cache lookups into filtering functions
- `config.py` - Added cache TTL configuration
- `config.ini` - Added cache TTL setting
- `main.py` - Pass cache to filtering pipeline

**Cache Methods:**
```python
# Search cache
cache.get_search_cache(media_type, title, year, ttl_days)
cache.set_search_cache(media_type, title, year, response_data, ttl_days)

# Details cache
cache.get_details_cache(media_type, media_id, ttl_days)
cache.set_details_cache(media_type, media_id, response_data, ttl_days)

# Cleanup
cache.cleanup_expired_cache()
```

### 6. Cache Invalidation

**Automatic Cleanup:**
- Runs at startup to remove expired entries
- TTL-based expiration (configurable in days)
- Prevents cache from growing indefinitely

**Cache Keys:**
- Include all search parameters for accuracy
- MD5 hashed for efficient storage and lookup
- Separate keys for search vs details data

### 7. Backward Compatibility

**Existing Cache:**
- Preserved during upgrade
- New tables added without affecting existing data
- Graceful fallback if cache is unavailable

### 8. Monitoring and Debugging

**Logging:**
- Cache hits logged as: `"Cache hit for movie search: 'The Matrix' (1999)"`
- Cache misses logged as: `"CACHE MISS: raw_title='The Matrix' key=abc123"`
- Performance metrics in filter statistics

## Usage

**No Configuration Changes Needed:**
- Caching is enabled by default
- 7-day TTL provides good balance of performance and freshness
- Automatically uses existing SQLite cache database

**Optional Configuration:**
```ini
# In config.ini
cache_ttl_days = 7  # Adjust as needed (1-30 days typical)
```

## Benefits

1. **Massive Speed Improvement**: 70-80% reduction in processing time
2. **Reduced API Load**: Fewer requests means less rate limiting
3. **Better Reliability**: Cached responses prevent failures during API issues
4. **Cost Effective**: Less API usage if you have rate limits or costs
5. **Scalable**: Performance improvement scales with library size

## Future Enhancements

Potential improvements that could be added:
- Cache warming (pre-populate cache with popular titles)
- Distributed cache for multi-instance deployments
- Cache compression for storage optimization
- Smart cache refresh (refresh popular items before expiration)

## Testing

To verify the caching is working:
1. Run the pipeline with logging enabled
2. Look for "Cache hit" messages in logs
3. Compare processing time with previous runs
4. Monitor API call frequency reduction

The aggressive caching system should provide immediate and significant performance improvements for your large VOD library processing.
