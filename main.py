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

PLAYER_CLIENT_COMBOS = [
    ['tv'],
    ['tv_embedded'],
    ['mediaconnect'],
    ['web_creator'],
    ['android_vr'],
    ['android_creator'],
    ['ios_creator'],
]

PIPED_INSTANCES = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.adminforge.de',
    'https://api.piped.yt',
    'https://pipedapi.r4fo.com',
    'https://pipedapi.leptons.xyz',
]

COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

yt_dlp_version = "unknown"
last_update_check = 0
UPDATE_INTERVAL = 3600


def update_ytdlp():
    global yt_dlp_version, last_update_check
    try:
        logger.info("Updating yt-dlp to latest version...")
        result = subprocess.run(
            ['pip', 'install', '--upgrade', '--force-reinstall', 'yt-dlp'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            import importlib
            importlib.reload(yt_dlp)
            yt_dlp_version = yt_dlp.version.__version__
            last_update_check = time.time()
            logger.info(f"yt-dlp updated to {yt_dlp_version}")
            return True
        else:
            logger.error(f"yt-dlp update failed: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"yt-dlp update error: {e}")
        return False


def check_and_update_ytdlp():
    global last_update_check
    now = time.time()
    if now - last_update_check > UPDATE_INTERVAL:
        update_ytdlp()


def get_ytdlp_version():
    try:
        return yt_dlp.version.__version__
    except:
        return "unknown"


try:
    yt_dlp_version = get_ytdlp_version()
    logger.info(f"yt-dlp version on start: {yt_dlp_version}")
except:
    pass

update_ytdlp()


def _get_ydl_opts(player_combo=None):
    if player_combo is None:
        player_combo = PLAYER_CLIENT_COMBOS[0]
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
    if player_combo:
        opts['extractor_args'] = {'youtube': {'player_client': player_combo}}
    return opts


def _download_via_piped(video_id, session_dir):
    for instance in PIPED_INSTANCES:
        try:
            logger.info(f"Trying Piped instance: {instance}")
            resp = req_lib.get(
                f"{instance}/streams/{video_id}",
                timeout=15,
                headers={'User-Agent': random.choice(USER_AGENTS)}
            )
            if resp.status_code != 200:
                logger.warning(f"Piped {instance} returned {resp.status_code}")
                continue

            data = resp.json()
            audio_streams = data.get('audioStreams', [])
            if not audio_streams:
                logger.warning(f"Piped {instance}: no audio streams")
                continue

            audio_streams.sort(key=lambda x: x.get('bitrate', 0), reverse=True)

            best_stream = None
            for stream in audio_streams:
                mime = stream.get('mimeType', '')
                if 'audio' in mime:
                    best_stream = stream
                    break
            if not best_stream:
                best_stream = audio_streams[0]

            stream_url = best_stream.get('url', '')
            if not stream_url:
                continue

            title = data.get('title', video_id)
            title = re.sub(r'[<>:"/\\|?*]', '', title).strip() or video_id

            ext = 'm4a'
            mime = best_stream.get('mimeType', '')
            if 'webm' in mime or 'opus' in mime:
                ext = 'webm'
            elif 'mp4' in mime or 'm4a' in mime:
                ext = 'm4a'

            raw_path = os.path.join(session_dir, f"{title}.{ext}")
            mp3_path = os.path.join(session_dir, f"{title}.mp3")

            logger.info(f"Downloading from Piped: bitrate={best_stream.get('bitrate')}, mime={mime}")
            audio_resp = req_lib.get(stream_url, stream=True, timeout=60,
                                     headers={'User-Agent': random.choice(USER_AGENTS)})
            if audio_resp.status_code != 200:
                logger.warning(f"Piped stream download failed: {audio_resp.status_code}")
                continue

            with open(raw_path, 'wb') as f:
                for chunk in audio_resp.iter_content(chunk_size=262144):
                    if chunk:
                        f.write(chunk)

            if os.path.getsize(raw_path) < 10000:
                logger.warning("Piped download too small, skipping")
                os.remove(raw_path)
                continue

            try:
                ffmpeg_result = subprocess.run(
                    ['ffmpeg', '-i', raw_path, '-vn', '-ab', '192k',
                     '-ar', '44100', '-y', mp3_path],
                    capture_output=True, text=True, timeout=120
                )
                if ffmpeg_result.returncode == 0 and os.path.exists(mp3_path):
                    os.remove(raw_path)
                    logger.info(f"Piped download + convert success: {title}")
                    return True
                else:
                    logger.warning(f"FFmpeg convert failed, using raw file")
                    return True
            except Exception as e:
                logger.warning(f"FFmpeg error: {e}, using raw file")
                return True

        except Exception as e:
            logger.warning(f"Piped {instance} failed: {str(e)[:100]}")
            continue

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


def _auto_update_loop():
    while True:
        time.sleep(UPDATE_INTERVAL)
        try:
            update_ytdlp()
        except:
            pass


ping_thread = threading.Thread(target=_self_ping, daemon=True)
ping_thread.start()

update_thread = threading.Thread(target=_auto_update_loop, daemon=True)
update_thread.start()


@app.route('/')
def home():
    ver = get_ytdlp_version()
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
            </div>
            <p class="version">yt-dlp: {ver} | Piped fallback: enabled | Auto-update: every 1 hour</p>
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
        'last_update': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_update_check)),
        'cookies_loaded': os.path.exists(COOKIES_FILE),
        'piped_fallback': True,
        'piped_instances': len(PIPED_INSTANCES),
        'player_clients': PLAYER_CLIENT_COMBOS
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


@app.route('/api/download')
def download():
    video_id = flask_request.args.get('v', '').strip()
    if not video_id or not re.match(r'^[A-Za-z0-9_-]{1,20}$', video_id):
        return jsonify({'error': 'Invalid video ID'}), 400

    req_title = flask_request.args.get('title', '').strip()

    session_id = uuid.uuid4().hex[:8]
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)

    check_and_update_ytdlp()

    errors_log = []
    method_used = None

    try:
        for combo in PLAYER_CLIENT_COMBOS:
            try:
                logger.info(f"Trying client {combo} for {video_id}")
                opts = _get_ydl_opts(combo)
                opts.update({
                    'format': 'bestaudio[ext=m4a]/bestaudio[acodec=mp4a]/bestaudio/best',
                    'writethumbnail': True,
                    'postprocessors': [
                        {
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '192',
                        },
                        {
                            'key': 'EmbedThumbnail',
                        },
                        {
                            'key': 'FFmpegMetadata',
                        },
                    ],
                    'outtmpl': os.path.join(session_dir, '%(title)s.%(ext)s'),
                })

                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)

                mp3_check = globmod.glob(os.path.join(session_dir, '*.mp3'))
                if mp3_check:
                    logger.info(f"yt-dlp success with client {combo}")
                    method_used = f"yt-dlp ({combo[0]})"
                    break
            except Exception as e:
                error_msg = str(e)
                errors_log.append(f"{combo}: {error_msg[:100]}")
                logger.warning(f"Client {combo} failed: {error_msg[:150]}")
                error_lower = error_msg.lower()
                if any(k in error_lower for k in ['403', 'forbidden', 'sign in', 'bot', 'confirm']):
                    time.sleep(0.5)
                    continue
                continue

        found_files = globmod.glob(os.path.join(session_dir, '*.mp3'))
        if not found_files:
            logger.info(f"yt-dlp failed for {video_id}, trying Piped API fallback...")
            piped_success = _download_via_piped(video_id, session_dir)
            if piped_success:
                method_used = "piped"

        mp3_files = globmod.glob(os.path.join(session_dir, '*.mp3'))
        if not mp3_files:
            all_files = globmod.glob(os.path.join(session_dir, '*'))
            audio_exts = ['.m4a', '.webm', '.opus', '.ogg', '.wav']
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
            logger.error(f"All methods failed for {video_id}: {errors_log}")
            return jsonify({
                'error': 'Download failed - all methods failed',
                'details': errors_log[-3:] if errors_log else [],
                'ytdlp_version': get_ytdlp_version(),
                'tip': 'Try /api/update-ytdlp to force update'
            }), 500

    except Exception as e:
        _cleanup_dir(session_dir)
        return jsonify({'error': str(e)[:200]}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Music Player API Server starting on port {port}...")
    logger.info(f"yt-dlp version: {get_ytdlp_version()}")
    logger.info(f"Piped fallback: {len(PIPED_INSTANCES)} instances configured")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
