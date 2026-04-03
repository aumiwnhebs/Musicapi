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
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0',
]

PLAYER_CLIENT_COMBOS = [
    ['default', 'tv', 'tv_embedded'],
    ['tv', 'default'],
    ['tv_embedded', 'tv'],
    ['default', 'mweb'],
    ['mweb', 'tv'],
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


def _get_common_ydl_opts(player_combo=None):
    if player_combo is None:
        player_combo = PLAYER_CLIENT_COMBOS[0]
    opts = {
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 60,
        'source_address': '0.0.0.0',
        'skip_unavailable_fragments': True,
        'fragment_retries': 10,
        'retries': 5,
        'extractor_args': {'youtube': {'player_client': player_combo}},
        'age_limit': 100,
        'nocheckcertificate': True,
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


def _download_with_ytdlp(ydl_opts, url):
    last_error = None
    for combo in PLAYER_CLIENT_COMBOS:
        try:
            retry_opts = dict(ydl_opts)
            retry_opts['extractor_args'] = {'youtube': {'player_client': combo}}
            retry_opts['http_headers'] = {
                'User-Agent': random.choice(USER_AGENTS),
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Sec-Fetch-Mode': 'navigate',
            }
            logger.info(f"Trying combo {combo}")
            with yt_dlp.YoutubeDL(retry_opts) as ydl:
                return ydl.extract_info(url, download=True)
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            logger.warning(f"Combo {combo} failed: {str(e)[:150]}")
            if '403' in error_str or 'forbidden' in error_str or 'format' in error_str or 'sign in' in error_str or 'bot' in error_str:
                time.sleep(1)
                continue
            raise
    raise last_error


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
            </div>
            <p class="version">yt-dlp: {ver} | Cookies: {cookies_status}</p>
        </div>
    </body>
    </html>
    '''


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'ytdlp_version': get_ytdlp_version()})


@app.route('/api/update-ytdlp')
def force_update():
    old_ver = get_ytdlp_version()
    success = update_ytdlp()
    new_ver = get_ytdlp_version()
    return jsonify({
        'success': success,
        'old_version': old_ver,
        'new_version': new_ver,
    })


@app.route('/api/download')
def download():
    video_id = flask_request.args.get('v', '').strip()
    if not video_id or not re.match(r'^[A-Za-z0-9_-]{1,20}$', video_id):
        return jsonify({'error': 'Invalid video ID'}), 400

    session_id = uuid.uuid4().hex[:8]
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)

    try:
        logger.info(f"=== Download request for {video_id} ===")

        video_url = f"https://www.youtube.com/watch?v={video_id}"

        ydl_opts = _get_common_ydl_opts()
        ydl_opts.update({
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': os.path.join(session_dir, '%(id)s.%(ext)s'),
            'nopostoverwrites': False,
            'buffersize': 1024,
            'http_chunk_size': 1048576,
        })

        info = _download_with_ytdlp(ydl_opts, video_url)
        title = info.get('title', 'audio')
        actual_id = info.get('id', video_id)

        logger.info(f"Download complete for {title}")

        filename = os.path.join(session_dir, f"{actual_id}.mp3")

        if not os.path.exists(filename):
            all_files = globmod.glob(os.path.join(session_dir, '*.mp3'))
            if all_files:
                filename = all_files[0]

        if not os.path.exists(filename):
            all_files = globmod.glob(os.path.join(session_dir, '*'))
            useful = [f for f in all_files if not f.endswith(('.part', '.jpg', '.png', '.webp'))]
            if useful:
                filename = useful[0]

        if os.path.exists(filename):
            filesize = os.path.getsize(filename)
            safe_title = re.sub(r'[<>:"/\\|?*]', '', title).strip() or 'download'
            safe_title = safe_title.encode('ascii', 'ignore').decode('ascii') or 'download'
            dl_filename = f"{safe_title}.mp3"
            logger.info(f"Serving {dl_filename} ({filesize} bytes)")

            def generate():
                try:
                    with open(filename, 'rb') as f:
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
                    'Content-Disposition': f'attachment; filename="{dl_filename}"',
                    'Content-Length': str(filesize),
                }
            )
        else:
            _cleanup_dir(session_dir)
            return jsonify({'error': 'Download failed: no file produced'}), 500

    except Exception as e:
        _cleanup_dir(session_dir)
        error_msg = str(e)[:200]
        logger.error(f"Download failed for {video_id}: {error_msg}")
        return jsonify({'error': error_msg}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Music Player API Server starting on port {port}...")
    logger.info(f"yt-dlp version: {get_ytdlp_version()}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
