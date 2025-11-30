# StrmSync - IPTV VOD to .strm Converter

## 🙏 Credits

Forked from [sftech13/m3u2strm](https://github.com/sftech13/m3u2strm) by sftech13.  
Enhanced with Jellyfin integration, web interface, folder comparison for duplicate cleanup, advanced keyword filtering, documentary support, and more.

A sophisticated Python tool that converts IPTV VOD playlists into `.strm` files for media servers like Jellyfin and Emby, with intelligent filtering, caching, and library management.

## 🚀 Features

### Core Functionality
- **M3U Playlist Processing**: Parse local files or remote URLs containing IPTV VOD content
- **Smart Content Filtering**: Use TMDb API to filter content by country of origin
- **Automatic .strm Generation**: Create properly formatted `.strm` files for media servers
- **Library Integration**: Automatically refresh Emby and Jellyfin libraries after updates
- **Multi-threaded Processing**: Parallel processing for faster performance

### Content Management
- **Movie Support**: Full movie library organization with year detection
- **TV Show Support**: Automatic season/episode parsing and folder structure
- **Documentary Support**: Separate categorization for documentary content
- **Live TV Filtering**: Automatically exclude REPLAY/live TV channels
- **Duplicate Detection**: Smart deduplication of identical entries

### Advanced Library Management 🆕
- **Content Quality Scoring**: Rate streams based on resolution, uptime, response time, and error rate
- **Stream Health Monitoring**: Track availability and performance of all streams in real-time
- **Automatic Stream Replacement**: Find alternative streams when primary ones fail
- **Library Analytics**: Comprehensive statistics and insights about your media library
- **Health Dashboard**: Visual monitoring of library health and performance trends
- **Low Quality Alerts**: Identify streams that need attention or replacement

### Advanced Features
- **SQLite Caching**: Persistent cache to avoid redundant API calls and processing
- **Local Media Detection**: Skip creating .strm files for content you already own
- **Keyword-based Filtering**: Customizable content categorization and exclusion
- **Dry Run Mode**: Test configurations without making actual changes
- **Comprehensive Logging**: Detailed logs for troubleshooting and monitoring
- **Folder Comparison**: Recursively compare folders and delete duplicates from output

## 📋 Requirements

- Python 3.8+
- TMDb API key (free from [The Movie Database](https://www.themoviedb.org/settings/api))
- Emby/Jellyfin API key (optional, for automatic library refresh)

## 🛠 Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/evilgenx/StrmSync.git
   cd StrmSync
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure the application**:
   - Copy `config.ini` to your preferred location
   - Update the configuration with your paths and API keys

## ⚙️ Configuration

Create a `config.ini` file with the following structure:

```ini
[paths]
# M3U source (local file or URL)
m3u = "/path/to/your/playlist.m3u"
# or m3u = "https://your-iptv-provider.com/playlist.m3u"

# Output directory for .strm files
output_dir = "/media/m3u2strm"

# Existing media directories (comma-separated)
existing_media_dirs = "/media/movies,/media/tv"

# Folders to compare with output for duplicate detection
compare_movies_dir = "/path/to/movies/to/compare"
compare_tv_dir = "/path/to/tv/to/compare"

# Cache and log files
sqlite_cache_file = "/path/to/cache.db"
log_file = "/path/to/m3u2strm.log"

[api]
# TMDb API key (required)
tmdb_api = "your_tmdb_api_key_here"

# Emby API (optional)
emby_api_url = "http://your-emby-server:8096"
emby_api_key = "your_emby_api_key"

# Jellyfin API (optional)
jellyfin_api_url = "http://your-jellyfin-server:8096"
jellyfin_api_key = "your_jellyfin_api_key"

[countries]
# Allowed countries for content filtering
allowed_movie_countries = US,GB,CA
allowed_tv_countries = US,GB,CA

[keywords]
# Content categorization keywords
tv_group_keywords = series,tv show,season,episode
doc_group_keywords = documentary,docu,bbc,netflix documentary
movie_group_keywords = movie,film
replay_group_keywords = replay,live

[ignore]
# Keywords to exclude from processing
tvshows = reality,news,sports
movies = adult,xxx

[settings]
# Operational settings
dry_run = false
max_workers = 8
write_non_us_report = true
```

### Configuration Details

#### Paths Section
- `m3u`: Path to your M3U playlist file or URL
- `output_dir`: Where .strm files will be created
- `existing_media_dirs`: Directories containing your local media (comma-separated)
- `sqlite_cache_file`: Database file for caching results
- `log_file`: Application log file location

#### API Section
- `tmdb_api`: Required for country filtering and metadata
- `emby_api_url` & `emby_api_key`: Optional for automatic Emby library refresh
- `jellyfin_api_url` & `jellyfin_api_key`: Optional for automatic Jellyfin library refresh

#### Countries Section
- `allowed_movie_countries`: Countries allowed for movies (comma-separated ISO codes)
- `allowed_tv_countries`: Countries allowed for TV shows

#### Keywords Section
- Content categorization keywords help identify movie/TV/documentary content
- Case-insensitive matching against M3U entry titles

#### Ignore Section
- Keywords that will exclude content from processing entirely

#### Settings Section
- `dry_run`: Test mode (no files created)
- `max_workers`: Thread count for parallel processing ("max" for CPU count)
- `write_non_us_report`: Generate excluded content report

#### Advanced Library Management Section
- `enable_quality_scoring`: Enable content quality scoring (true/false)
- `enable_health_monitoring`: Enable stream health monitoring (true/false)
- `enable_auto_replacement`: Enable automatic stream replacement (true/false)
- `enable_analytics`: Enable library analytics (true/false)
- `health_check_interval`: Health check interval in seconds (default: 3600)
- `health_check_timeout`: Health check timeout in seconds (default: 10)
- `health_check_mode`: Health check sampling mode ('all', 'random', 'percentage') (default: 'random')
- `health_check_sample_size`: Number of random files to test per cycle (default: 50)
- `health_check_sample_percentage`: Percentage of library to test (0.0-1.0, used when mode='percentage') (default: 0.1)
- `resolution_weight`: Weight for resolution in quality scoring (default: 0.4)
- `uptime_weight`: Weight for uptime in quality scoring (default: 0.3)
- `response_time_weight`: Weight for response time in quality scoring (default: 0.2)
- `error_rate_weight`: Weight for error rate in quality scoring (default: 0.1)
- `min_quality_threshold`: Minimum quality score for stream replacement (default: 5.0)

## 🚀 Usage

### Basic Usage
```bash
python main.py
```

### Dry Run (Test Mode)
Set `dry_run = true` in config.ini to test without creating files.

### Manual Run with Custom Config
```bash
python main.py --config /path/to/custom_config.ini
```

### Health Monitoring with Random Sampling
The system now supports intelligent health monitoring with random sampling to efficiently check stream availability:

```bash
# Run background health monitoring (daemon mode)
python main.py --background-health
```

#### Sampling Modes:
- **Random Mode** (default): Test a random selection of streams per cycle
- **All Mode**: Test all streams (resource-intensive)
- **Percentage Mode**: Test a percentage of your library

#### Configuration Options:
```ini
[library_management]
# Health check sampling settings
health_check_mode = random          # 'all', 'random', or 'percentage'
health_check_sample_size = 50       # Number of random files to test
health_check_sample_percentage = 0.1 # 10% of total library (when mode=percentage)
health_check_interval = 3600        # Check every hour
```

#### Benefits of Random Sampling:
- **Efficient**: Tests only a subset of your library per cycle
- **Comprehensive**: Over time, all streams get tested through rotation
- **Configurable**: Control how aggressive testing should be
- **Resource-Friendly**: Reduces network load and processing time

#### Example Scenarios:
```ini
# Light monitoring: Test 20 random files every 2 hours
health_check_mode = random
health_check_sample_size = 20
health_check_interval = 7200

# Heavy monitoring: Test 20% of library every hour
health_check_mode = percentage
health_check_sample_percentage = 0.2
health_check_interval = 3600

# Complete monitoring: Test all files every 6 hours (use with caution)
health_check_mode = all
health_check_interval = 21600
```

### Folder Comparison and Duplicate Deletion
The tool can recursively compare folders and delete duplicates from the output directory:

```bash
# Compare folders and delete duplicates (with confirmation)
python main.py --compare-folders

# Generate a report of duplicate folders without deleting
python main.py --report

# Run folder comparison in dry-run mode (preview only)
# Set dry_run = true in config.ini, then run:
python main.py --compare-folders
```

#### Folder Comparison Features:
- **Recursive Comparison**: Compares folder structures deeply at all levels
- **Same Relative Path Matching**: Identifies folders with identical names at identical relative paths
- **Category-Specific**: Compare Movies and TV Shows separately
- **Safe Deletion**: Only deletes from output directory, never from comparison directories
- **Interactive Confirmation**: Requires user confirmation before deleting any folders
- **Dry Run Mode**: Preview what would be deleted without making changes

## 📁 File Structure

```
StrmSync/
├── main.py              # Main orchestration script
├── config.py            # Configuration management
├── core.py              # Core logic: media scanning, caching, title normalization
├── m3u_utils.py         # M3U parsing and TMDb filtering
├── strm_utils.py        # .strm file creation and cleanup
├── url_utils.py         # URL handling utilities
├── config.ini           # Configuration template
├── requirements.txt     # Python dependencies
└── readme.md           # This file
```

### Output Structure
```
/media/m3u2strm/
├── Movies/
│   ├── Heat (1995)/
│   │   └── Heat (1995).strm
│   └── Inception (2010)/
│       └── Inception (2010).strm
└── TV Shows/
    ├── Breaking Bad (2008)/
    │   └── Season 01/
    │       ├── Breaking Bad (2008) S01E01.strm
    │       └── Breaking Bad (2008) S01E02.strm
    └── The Office (2005)/
        └── Season 02/
            └── The Office (2005) S02E03.strm
```

## 🔧 Advanced Configuration

### Country Filtering
The tool uses TMDb API to determine the country of origin for each title. Only content from allowed countries is processed.

Example countries: `US` (United States), `GB` (United Kingdom), `CA` (Canada), `AU` (Australia), `DE` (Germany)

### Keyword Groups
- **TV Group**: Identifies TV shows (e.g., "series", "tv show", "season")
- **Documentary Group**: Identifies documentaries (e.g., "documentary", "docu")
- **Movie Group**: Identifies movies (e.g., "movie", "film")
- **Replay Group**: Identifies live TV to exclude (e.g., "replay", "live")

### Ignore Lists
Prevent specific content from being processed:
- `tvshows`: TV show keywords to ignore
- `movies`: Movie keywords to ignore

## 🐛 Troubleshooting

### Common Issues

**TMDb API Errors**
- Verify your API key is correct and active
- Check rate limits (free tier has daily limits)
- Ensure your IP isn't blocked

**Permission Errors**
- Ensure write permissions to output directories
- Check that existing media directories are accessible

**Cache Issues**
- Delete the SQLite cache file to force full reprocessing
- Cache location: `sqlite_cache_file` in config

**Missing Content**
- Check `excluded_entries.txt` for filtered content
- Verify country settings match your preferences
- Review keyword categorization settings

### Log Files
Check the log file specified in `log_file` for detailed processing information:
- Processing progress and statistics
- API call results and errors
- File creation and cleanup operations

## 🔄 How It Works

1. **Scan Local Media**: Build cache of existing movies and TV shows
2. **Parse M3U Playlist**: Read and categorize VOD entries
3. **Country Filtering**: Use TMDb API to filter by allowed countries
4. **Deduplication**: Remove duplicate entries
5. **Cache Check**: Compare against existing cache and local media
6. **.strm Creation**: Generate .strm files for new/missing content
7. **Cleanup**: Remove orphaned .strm files
8. **Library Refresh**: Trigger Emby/Jellyfin library update (if configured)

## 📊 Performance Tips

- Use `max_workers = "max"` to utilize all CPU cores
- Place cache file on fast storage (SSD preferred)
- Use local M3U files instead of URLs when possible
- Regularly clean up orphaned .strm files with the built-in cleanup

## 🤝 Contributing

Contributions are welcome! Please feel free to submit pull requests or open issues for bugs and feature requests.

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🙏 Acknowledgments

- The Movie Database (TMDb) for content metadata
- IPTV providers for VOD content
- Emby and Jellyfin communities for .strm file format support
