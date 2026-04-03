import os
import re
import uuid
import random
import time
import glob as globmod
import threading
import yt_dlp
from flask import Flask, jsonify, request as flask_request, Response

app = Flask(__name__)

DOWNLOAD_FOLDER = "api_downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
]

PLAYER_CLIENT_COMBOS = [
    ['android_vr'],
    ['android_creator'],
    ['android_music'],
    ['ios'],
    ['web'],
]


def _get_ydl_opts(player_combo=None):
    if player_combo is None:
        player_combo = PLAYER_CLIENT_COMBOS[0]
    opts = {
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 20,
        'source_address': '0.0.0.0',
        'skip_unavailable_fragments': True,
        'fragment_retries': 5,
        'retries': 3,
        'concurrent_fragment_downloads': 4,
        'buffersize': 1024 * 64,
        'age_limit': 100,
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        },
    }
    if player_combo:
        opts['extractor_args'] = {'youtube': {'player_client': player_combo}}
    return opts


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
    import requests
    render_url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if not render_url:
        return
    while True:
        try:
            requests.get(f"{render_url}/api/health", timeout=10)
        except:
            pass
        time.sleep(600)


ping_thread = threading.Thread(target=_self_ping, daemon=True)
ping_thread.start()


@app.route('/')
def home():
    return '''
    <html>
    <head>
        <title>Music Player API</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { background: #1a1a2e; color: #eee; font-family: Arial, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
            .container { text-align: center; padding: 40px; }
            h1 { color: #e94560; font-size: 2em; }
            p { color: #aaa; font-size: 1.1em; margin: 10px 0; }
            .status { color: #4ecca3; font-weight: bold; font-size: 1.3em; margin: 20px 0; }
            .endpoints { text-align: left; background: #16213e; padding: 20px; border-radius: 10px; margin-top: 20px; }
            .endpoints code { color: #e94560; }
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
            </div>
        </div>
    </body>
    </html>
    '''


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})


@app.route('/api/download')
def download():
    video_id = flask_request.args.get('v', '').strip()
    if not video_id or not re.match(r'^[A-Za-z0-9_-]{1,20}$', video_id):
        return jsonify({'error': 'Invalid video ID'}), 400

    req_title = flask_request.args.get('title', '').strip()

    session_id = uuid.uuid4().hex[:8]
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)

    try:
        for combo in PLAYER_CLIENT_COMBOS:
            try:
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

                song_title = req_title
                if not song_title:
                    try:
                        info_opts = _get_ydl_opts(combo)
                        info_opts['skip_download'] = True
                        with yt_dlp.YoutubeDL(info_opts) as ydl:
                            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                            song_title = info.get('title', video_id)
                    except:
                        song_title = video_id

                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
                    break
            except Exception as e:
                error_str = str(e).lower()
                if any(k in error_str for k in ['403', 'forbidden', 'drm', 'sign in', 'bot']):
                    time.sleep(0.5)
                    continue
                continue

        mp3_files = globmod.glob(os.path.join(session_dir, '*.mp3'))
        if not mp3_files:
            all_files = globmod.glob(os.path.join(session_dir, '*'))
            if all_files:
                mp3_files = all_files

        if mp3_files:
            filepath = mp3_files[0]
            filename = os.path.basename(filepath)
            filename = filename.encode('ascii', 'ignore').decode('ascii') or 'download.mp3'
            filesize = os.path.getsize(filepath)

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
            return jsonify({'error': 'Download failed'}), 500

    except Exception as e:
        _cleanup_dir(session_dir)
        return jsonify({'error': str(e)[:200]}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Music Player API Server starting on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
