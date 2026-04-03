import os
import re
import uuid
import random
import time
import glob as globmod
import threading
import subprocess
import logging
import requests as req_lib
import yt_dlp
from flask import Flask, jsonify, request as flask_request, Response

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOAD_FOLDER = "api_downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36',
]

COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

yt_dlp_version = "unknown"


def update_ytdlp():
    global yt_dlp_version
    try:
        logger.info("Checking yt-dlp for updates...")
        result = subprocess.run(
            ['pip', 'install', '--upgrade', 'yt-dlp'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            import importlib
            importlib.reload(yt_dlp)
            yt_dlp_version = yt_dlp.version.__version__
            logger.info(f"yt-dlp is at latest version: {yt_dlp_version}")
            return True
        else:
            logger.error(f"yt-dlp update failed: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"yt-dlp update error: {e}")
        return False


def get_ytdlp_version():
    try:
        return yt_dlp.version.__version__
    except:
        return "unknown"


yt_dlp_version = get_ytdlp_version()
logger.info(f"yt-dlp version on start: {yt_dlp_version}")

if os.path.exists(COOKIES_FILE):
    cookie_size = os.path.getsize(COOKIES_FILE)
    logger.info(f"Cookies file found: {COOKIES_FILE} ({cookie_size} bytes)")
else:
    logger.info(f"No cookies file at {COOKIES_FILE}")

update_ytdlp()


def _base_opts():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
        'source_address': '0.0.0.0',
        'skip_unavailable_fragments': True,
        'fragment_retries': 5,
        'retries': 3,
        'concurrent_fragment_downloads': 4,
        'buffersize': 1024 * 64,
        'age_limit': 100,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'geo_bypass_country': 'IN',
        'http_headers': {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        },
    }
    if os.path.exists(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
    return opts


def _get_download_strategies(session_dir):
    strategies = []

    strategies.append({
        'name': 'default_bestaudio',
        'opts': {
            'format': 'bestaudio/best',
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                {'key': 'EmbedThumbnail'},
                {'key': 'FFmpegMetadata'},
            ],
            'writethumbnail': True,
            'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
        }
    })

    strategies.append({
        'name': 'noformat_extract',
        'opts': {
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
            ],
            'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
        }
    })

    strategies.append({
        'name': 'any_format_raw',
        'opts': {
            'format': 'worstaudio/worst/bestaudio/best',
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'},
            ],
            'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
        }
    })

    strategies.append({
        'name': 'direct_download',
        'opts': {
            'format': 'best',
            'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
        }
    })

    return strategies


def _list_formats(video_id):
    try:
        opts = _base_opts()
        opts['skip_download'] = True
        opts['quiet'] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            formats = info.get('formats', [])
            format_summary = []
            for f in formats:
                fid = f.get('format_id', '?')
                ext = f.get('ext', '?')
                acodec = f.get('acodec', 'none')
                vcodec = f.get('vcodec', 'none')
                abr = f.get('abr', 0)
                format_summary.append(f"id={fid} ext={ext} a={acodec} v={vcodec} abr={abr}")
            return formats, format_summary
    except Exception as e:
        logger.warning(f"Format listing failed: {str(e)[:100]}")
        return [], []


def _convert_to_mp3(session_dir):
    all_files = globmod.glob(os.path.join(session_dir, '*'))
    non_mp3 = [f for f in all_files if not f.endswith('.mp3') and not f.endswith('.part') and not f.endswith('.jpg') and not f.endswith('.png') and not f.endswith('.webp')]
    for raw_path in non_mp3:
        mp3_path = os.path.splitext(raw_path)[0] + '.mp3'
        try:
            result = subprocess.run(
                ['ffmpeg', '-i', raw_path, '-vn', '-ab', '192k', '-ar', '44100', '-y', mp3_path],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0 and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 10000:
                os.remove(raw_path)
                logger.info(f"Converted {os.path.basename(raw_path)} to MP3")
                return True
        except Exception as e:
            logger.warning(f"FFmpeg convert error: {e}")
    return False


def _cleanup_dir(dirpath):
    try:
        for f in os.listdir(dirpath):
            filepath = os.path.join(dirpath, f)
            try:
                os.remove(filepath)
            except Exception:
                pass
        os.rmdir(dirpath)
    except Exception:
        pass


def _self_ping():
    render_url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if not render_url:
        return
    while True:
        try:
            req_lib.get(f"{render_url}/api/health", timeout=10)
        except:
            pass
        time.sleep(600)


ping_thread = threading.Thread(target=_self_ping, daemon=True)
ping_thread.start()


@app.route('/')
def home():
    ver = get_ytdlp_version()
    cookies_status = "loaded" if os.path.exists(COOKIES_FILE) else "not found"
    return f'''
    <html>
    <head>
        <title>Music Player API</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ background: #1a1a2e; color: #eee; font-family: Arial, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
            .container {{ text-align: center; padding: 40px; }}
            h1 {{ color: #e94560; font-size: 2em; }}
            p {{ color: #aaa; font-size: 1.1em; margin: 10px 0; }}
            .status {{ color: #4ecca3; font-weight: bold; font-size: 1.3em; margin: 20px 0; }}
            .endpoints {{ text-align: left; background: #16213e; padding: 20px; border-radius: 10px; margin-top: 20px; }}
            .endpoints code {{ color: #e94560; }}
            .version {{ color: #888; font-size: 0.9em; margin-top: 15px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Music Player API Server</h1>
            <div class="status">Server is Running</div>
            <p>This server powers the Music Player Android App</p>
            <div class="endpoints">
                <p><code>GET /api/health</code> - Server status</p>
                <p><code>GET /api/download?v=VIDEO_ID</code> - Download MP3</p>
                <p><code>GET /api/update-ytdlp</code> - Force update yt-dlp</p>
                <p><code>GET /api/version</code> - Check yt-dlp version</p>
                <p><code>GET /api/formats?v=VIDEO_ID</code> - List available formats</p>
            </div>
            <p class="version">yt-dlp: {ver} | Cookies: {cookies_status}</p>
        </div>
    </body>
    </html>
    '''


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'ytdlp_version': get_ytdlp_version()})


@app.route('/api/version')
def version():
    return jsonify({
        'ytdlp_version': get_ytdlp_version(),
        'cookies_loaded': os.path.exists(COOKIES_FILE),
    })


@app.route('/api/update-ytdlp')
def force_update():
    old_ver = get_ytdlp_version()
    success = update_ytdlp()
    new_ver = get_ytdlp_version()
    return jsonify({
        'success': success,
        'old_version': old_ver,
        'new_version': new_ver,
        'message': f'Updated from {old_ver} to {new_ver}' if success else 'Update failed'
    })


@app.route('/api/formats')
def formats():
    video_id = flask_request.args.get('v', '').strip()
    if not video_id or not re.match(r'^[A-Za-z0-9_-]{1,20}$', video_id):
        return jsonify({'error': 'Invalid video ID'}), 400
    fmts, summary = _list_formats(video_id)
    return jsonify({
        'video_id': video_id,
        'format_count': len(fmts),
        'formats': summary[:30],
        'has_audio': any(f.get('acodec', 'none') != 'none' for f in fmts),
        'has_video': any(f.get('vcodec', 'none') != 'none' for f in fmts),
    })


@app.route('/api/download')
def download():
    video_id = flask_request.args.get('v', '').strip()
    if not video_id or not re.match(r'^[A-Za-z0-9_-]{1,20}$', video_id):
        return jsonify({'error': 'Invalid video ID'}), 400

    session_id = uuid.uuid4().hex[:8]
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)

    errors_log = []
    method_used = None

    try:
        logger.info(f"=== Download request for {video_id} ===")

        fmts, fmt_summary = _list_formats(video_id)
        if fmts:
            logger.info(f"Found {len(fmts)} formats. Audio formats: {sum(1 for f in fmts if f.get('acodec', 'none') != 'none')}")
            audio_fmts = [f for f in fmts if f.get('acodec', 'none') != 'none']
            if audio_fmts:
                audio_fmts.sort(key=lambda x: x.get('abr', 0) or 0, reverse=True)
                best = audio_fmts[0]
                logger.info(f"Best audio: id={best.get('format_id')} ext={best.get('ext')} abr={best.get('abr')} acodec={best.get('acodec')}")
        else:
            logger.warning(f"No formats found for {video_id}")

        strategies = _get_download_strategies(session_dir)

        if fmts:
            audio_fmts = [f for f in fmts if f.get('acodec', 'none') != 'none']
            if audio_fmts:
                audio_fmts.sort(key=lambda x: x.get('abr', 0) or 0, reverse=True)
                best_id = audio_fmts[0].get('format_id')
                strategies.insert(1, {
                    'name': f'specific_format_{best_id}',
                    'opts': {
                        'format': best_id,
                        'postprocessors': [
                            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                        ],
                        'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
                    }
                })

            all_with_audio = [f for f in fmts if f.get('acodec', 'none') != 'none']
            if all_with_audio:
                format_ids = '/'.join(f.get('format_id', '') for f in all_with_audio[:5])
                strategies.insert(2, {
                    'name': 'multi_format_try',
                    'opts': {
                        'format': format_ids,
                        'postprocessors': [
                            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                        ],
                        'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
                    }
                })

        for strategy in strategies:
            mp3_check = globmod.glob(os.path.join(session_dir, '*.mp3'))
            if mp3_check:
                break

            try:
                name = strategy['name']
                logger.info(f"Trying strategy: {name}")
                opts = _base_opts()
                opts.update(strategy['opts'])

                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)

                mp3_check = globmod.glob(os.path.join(session_dir, '*.mp3'))
                if mp3_check:
                    logger.info(f"Strategy {name} succeeded!")
                    method_used = name
                    break

                all_files = globmod.glob(os.path.join(session_dir, '*'))
                if all_files:
                    logger.info(f"Strategy {name} downloaded files but no mp3, converting...")
                    if _convert_to_mp3(session_dir):
                        method_used = f"{name}+ffmpeg"
                        break

            except Exception as e:
                error_msg = str(e)
                errors_log.append(f"{strategy['name']}: {error_msg[:120]}")
                logger.warning(f"Strategy {strategy['name']} failed: {error_msg[:200]}")
                time.sleep(0.3)
                continue

        mp3_files = globmod.glob(os.path.join(session_dir, '*.mp3'))
        if not mp3_files:
            all_files = globmod.glob(os.path.join(session_dir, '*'))
            all_files = [f for f in all_files if not f.endswith('.part') and not f.endswith('.jpg') and not f.endswith('.png') and not f.endswith('.webp')]
            if all_files:
                _convert_to_mp3(session_dir)
                mp3_files = globmod.glob(os.path.join(session_dir, '*.mp3'))

            if not mp3_files:
                audio_exts = ['.m4a', '.webm', '.opus', '.ogg', '.wav', '.mp4', '.mkv']
                mp3_files = [f for f in all_files if any(f.lower().endswith(ext) for ext in audio_exts)]
                if not mp3_files and all_files:
                    mp3_files = all_files

        if mp3_files:
            filepath = mp3_files[0]
            filename = os.path.basename(filepath)
            filename = filename.encode('ascii', 'ignore').decode('ascii') or 'download.mp3'
            filesize = os.path.getsize(filepath)
            logger.info(f"Serving {filename} ({filesize} bytes) via {method_used}")

            def generate():
                try:
                    with open(filepath, 'rb') as f:
                        while True:
                            chunk = f.read(262144)
                            if not chunk:
                                break
                            yield chunk
                finally:
                    _cleanup_dir(session_dir)

            return Response(
                generate(),
                mimetype='audio/mpeg',
                headers={
                    'Content-Disposition': f'attachment; filename="{filename}"',
                    'Content-Length': str(filesize),
                }
            )
        else:
            _cleanup_dir(session_dir)
            logger.error(f"All strategies failed for {video_id}: {errors_log}")
            return jsonify({
                'error': 'Download failed',
                'details': errors_log[-3:] if errors_log else [],
                'ytdlp_version': get_ytdlp_version()
            }), 500

    except Exception as e:
        _cleanup_dir(session_dir)
        logger.error(f"Download exception for {video_id}: {str(e)[:200]}")
        return jsonify({'error': str(e)[:200]}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Music Player API Server starting on port {port}...")
    logger.info(f"yt-dlp version: {get_ytdlp_version()}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
