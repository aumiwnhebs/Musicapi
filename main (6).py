import os
import re
import uuid
import random
import time
import glob as globmod
import threading
import requests as req_lib
import yt_dlp
from flask import Flask, jsonify, request as flask_request, Response, send_file

app = Flask(__name__)

DOWNLOAD_FOLDER = "api_downloads"
STREAM_CACHE_FOLDER = "stream_cache"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(STREAM_CACHE_FOLDER, exist_ok=True)

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

from collections import OrderedDict

_stream_cache = OrderedDict()
_cache_lock = threading.Lock()
CACHE_TTL = 4 * 3600
CACHE_MAX_SIZE = 200

_file_cache = {}
_file_cache_lock = threading.Lock()
FILE_CACHE_TTL = 3600
_download_locks = {}
_download_locks_lock = threading.Lock()


def _cache_get(video_id):
    with _cache_lock:
        entry = _stream_cache.get(video_id)
        if entry is None:
            return None
        if (time.time() - entry['ts']) >= CACHE_TTL:
            del _stream_cache[video_id]
            return None
        _stream_cache.move_to_end(video_id)
        return entry['data']


def _cache_set(video_id, data):
    with _cache_lock:
        if video_id in _stream_cache:
            _stream_cache.move_to_end(video_id)
            _stream_cache[video_id] = {'data': data, 'ts': time.time()}
        else:
            if len(_stream_cache) >= CACHE_MAX_SIZE:
                _stream_cache.popitem(last=False)
            _stream_cache[video_id] = {'data': data, 'ts': time.time()}


def _get_cached_file(video_id):
    with _file_cache_lock:
        entry = _file_cache.get(video_id)
        if entry is None:
            return None
        if (time.time() - entry['ts']) >= FILE_CACHE_TTL:
            try:
                os.remove(entry['path'])
            except Exception:
                pass
            del _file_cache[video_id]
            return None
        return entry['path']


def _set_cached_file(video_id, filepath):
    with _file_cache_lock:
        _file_cache[video_id] = {'path': filepath, 'ts': time.time()}


def _cleanup_old_stream_files():
    with _file_cache_lock:
        expired = []
        for vid, entry in _file_cache.items():
            if (time.time() - entry['ts']) >= FILE_CACHE_TTL:
                expired.append(vid)
        for vid in expired:
            try:
                os.remove(_file_cache[vid]['path'])
            except Exception:
                pass
            del _file_cache[vid]


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


def _get_stream_url(video_id):
    cached = _cache_get(video_id)
    if cached:
        return cached

    for combo in PLAYER_CLIENT_COMBOS:
        try:
            opts = _get_ydl_opts(combo)
            opts['format'] = 'bestaudio[ext=m4a][acodec=mp4a]/bestaudio[ext=m4a]/bestaudio[acodec=aac]/bestaudio/best'
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                url = info.get('url')
                fmt_ext = info.get('ext', 'unknown')
                fmt_acodec = info.get('acodec', 'unknown')
                http_headers = info.get('http_headers', {})
                if url:
                    print(f"[stream] {video_id}: ext={fmt_ext} acodec={fmt_acodec}")
                    result = {
                        'url': url,
                        'title': info.get('title', 'Unknown'),
                        'duration': info.get('duration', 0),
                        'thumbnail': info.get('thumbnail', ''),
                        'format': fmt_ext,
                        'http_headers': http_headers,
                    }
                    _cache_set(video_id, result)
                    return result
                formats = info.get('formats', [])
                aac_formats = [f for f in formats if f.get('acodec', 'none') != 'none' and 'mp4a' in (f.get('acodec') or '')]
                if not aac_formats:
                    aac_formats = [f for f in formats if f.get('acodec', 'none') != 'none']
                aac_formats.sort(key=lambda f: f.get('abr', 0) or 0, reverse=True)
                if aac_formats:
                    chosen = aac_formats[0]
                    print(f"[stream] {video_id}: fallback ext={chosen.get('ext')} acodec={chosen.get('acodec')}")
                    result = {
                        'url': chosen['url'],
                        'title': info.get('title', 'Unknown'),
                        'duration': info.get('duration', 0),
                        'thumbnail': info.get('thumbnail', ''),
                        'format': chosen.get('ext', 'unknown'),
                        'http_headers': http_headers,
                    }
                    _cache_set(video_id, result)
                    return result
        except Exception as e:
            error_str = str(e).lower()
            if any(k in error_str for k in ['403', 'forbidden', 'drm', 'sign in', 'bot']):
                time.sleep(0.5)
                continue
            continue
    return None


def _get_video_lock(video_id):
    with _download_locks_lock:
        if video_id not in _download_locks:
            _download_locks[video_id] = threading.Lock()
        return _download_locks[video_id]


def _download_stream_to_file(video_id):
    cached_path = _get_cached_file(video_id)
    if cached_path and os.path.exists(cached_path):
        return cached_path

    lock = _get_video_lock(video_id)
    with lock:
        cached_path = _get_cached_file(video_id)
        if cached_path and os.path.exists(cached_path):
            return cached_path

        try:
            outtmpl = os.path.join(STREAM_CACHE_FOLDER, f"{video_id}.%(ext)s")
            for combo in PLAYER_CLIENT_COMBOS:
                try:
                    opts = _get_ydl_opts(combo)
                    opts['format'] = 'bestaudio[ext=m4a][acodec=mp4a]/bestaudio[ext=m4a]/bestaudio[acodec=aac]/bestaudio/best'
                    opts['outtmpl'] = outtmpl
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
                        ext = info.get('ext', 'm4a')
                        final_path = os.path.join(STREAM_CACHE_FOLDER, f"{video_id}.{ext}")
                        if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
                            _set_cached_file(video_id, final_path)
                            print(f"[proxy] Downloaded {video_id}: {os.path.getsize(final_path)} bytes")
                            return final_path
                except Exception as e:
                    error_str = str(e).lower()
                    print(f"[proxy] yt-dlp download attempt failed for {video_id}: {str(e)[:100]}")
                    if any(k in error_str for k in ['403', 'forbidden', 'drm', 'sign in', 'bot']):
                        time.sleep(0.5)
                        continue
                    continue

            for f in os.listdir(STREAM_CACHE_FOLDER):
                if f.startswith(video_id + '.') and not f.endswith('.tmp'):
                    fpath = os.path.join(STREAM_CACHE_FOLDER, f)
                    if os.path.getsize(fpath) > 0:
                        _set_cached_file(video_id, fpath)
                        print(f"[proxy] Found downloaded {video_id}: {fpath}")
                        return fpath

            print(f"[proxy] All download attempts failed for {video_id}")
            return None
        except Exception as e:
            print(f"[proxy] Download error for {video_id}: {str(e)[:200]}")
            return None


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
                <p><code>GET /api/stream?v=VIDEO_ID</code> - Get audio stream URL</p>
                <p><code>GET /api/proxy-stream?v=VIDEO_ID</code> - Proxy audio stream</p>
                <p><code>GET /api/download?v=VIDEO_ID</code> - Download MP3</p>
            </div>
        </div>
    </body>
    </html>
    '''


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})


_active_notification = None

@app.route('/api/notification', methods=['GET', 'POST', 'DELETE'])
def notification():
    global _active_notification

    if flask_request.method == 'POST':
        data = flask_request.get_json(silent=True) or {}
        heading = data.get('heading', '').strip()
        message = data.get('message', '').strip()
        if not heading or not message:
            return jsonify({'error': 'Both heading and message are required'}), 400
        _active_notification = {
            'heading': heading,
            'message': message,
            'timestamp': int(time.time() * 1000)
        }
        print(f"[notification] Set: {heading} - {message}")
        return jsonify({'status': 'ok', 'notification': _active_notification})

    if flask_request.method == 'DELETE':
        _active_notification = None
        print("[notification] Cleared")
        return jsonify({'status': 'ok', 'message': 'Notification cleared'})

    if _active_notification:
        return jsonify({'status': 'ok', 'notification': _active_notification})
    return jsonify({'status': 'ok', 'notification': None})


@app.route('/admin/notify')
def admin_notify_page():
    return '''
    <html>
    <head>
        <title>Send Notification</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body { background: #1a1a2e; color: #eee; font-family: Arial, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
            .container { width: 90%; max-width: 500px; padding: 30px; }
            h1 { color: #e94560; font-size: 1.6em; text-align: center; margin-bottom: 30px; }
            label { display: block; color: #aaa; font-size: 14px; margin-bottom: 6px; margin-top: 18px; }
            input, textarea { width: 100%; padding: 12px 14px; background: #16213e; border: 1px solid #333; border-radius: 10px; color: #fff; font-size: 15px; outline: none; }
            input:focus, textarea:focus { border-color: #e94560; }
            textarea { resize: vertical; min-height: 80px; }
            .btn { width: 100%; padding: 14px; border: none; border-radius: 12px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 20px; }
            .btn-send { background: #e94560; color: white; }
            .btn-send:hover { background: #d63851; }
            .btn-clear { background: #333; color: #aaa; }
            .btn-clear:hover { background: #444; }
            #result { margin-top: 16px; padding: 12px; border-radius: 10px; text-align: center; font-size: 14px; display: none; }
            .success { background: #1b4332; color: #4ecca3; }
            .error { background: #4a1525; color: #e94560; }
            .current { margin-top: 24px; padding: 16px; background: #16213e; border-radius: 12px; }
            .current h3 { color: #4ecca3; font-size: 14px; margin-bottom: 10px; }
            .current .heading { color: #fff; font-weight: bold; font-size: 15px; }
            .current .message { color: #aaa; font-size: 13px; margin-top: 4px; }
            .current .none { color: #555; font-style: italic; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Send Notification</h1>

            <label>Heading (Bold Text)</label>
            <input type="text" id="heading" placeholder="e.g. New Update Available!">

            <label>Message (Normal Text)</label>
            <textarea id="message" placeholder="e.g. Version 2.0 is here with better features"></textarea>

            <button class="btn btn-send" onclick="sendNotif()">Send Notification</button>
            <button class="btn btn-clear" onclick="clearNotif()">Clear Notification</button>

            <div id="result"></div>

            <div class="current" id="currentNotif">
                <h3>CURRENT NOTIFICATION</h3>
                <div id="currentContent"><span class="none">Loading...</span></div>
            </div>
        </div>

        <script>
            function showResult(msg, isError) {
                const r = document.getElementById('result');
                r.textContent = msg;
                r.className = isError ? 'error' : 'success';
                r.style.display = 'block';
                setTimeout(() => r.style.display = 'none', 4000);
            }

            async function sendNotif() {
                const heading = document.getElementById('heading').value.trim();
                const message = document.getElementById('message').value.trim();
                if (!heading || !message) { showResult('Both fields are required', true); return; }
                try {
                    const res = await fetch('/api/notification', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({heading, message})
                    });
                    const data = await res.json();
                    if (data.status === 'ok') {
                        showResult('Notification sent successfully!', false);
                        document.getElementById('heading').value = '';
                        document.getElementById('message').value = '';
                        loadCurrent();
                    } else {
                        showResult(data.error || 'Failed', true);
                    }
                } catch(e) { showResult('Error: ' + e.message, true); }
            }

            async function clearNotif() {
                try {
                    await fetch('/api/notification', {method: 'DELETE'});
                    showResult('Notification cleared', false);
                    loadCurrent();
                } catch(e) { showResult('Error: ' + e.message, true); }
            }

            async function loadCurrent() {
                try {
                    const res = await fetch('/api/notification');
                    const data = await res.json();
                    const el = document.getElementById('currentContent');
                    if (data.notification) {
                        el.innerHTML = '<div class="heading">' + data.notification.heading + '</div><div class="message">' + data.notification.message + '</div>';
                    } else {
                        el.innerHTML = '<span class="none">No active notification</span>';
                    }
                } catch(e) {}
            }

            loadCurrent();
        </script>
        </div>
    </body>
    </html>
    '''


_bg_downloads = {}
_bg_downloads_lock = threading.Lock()


def _bg_download_file(video_id):
    with _bg_downloads_lock:
        if video_id in _bg_downloads and _bg_downloads[video_id].get('status') in ('running', 'done'):
            return
        _bg_downloads[video_id] = {'status': 'running', 'path': None}

    def do_download():
        try:
            path = _download_stream_to_file(video_id)
            with _bg_downloads_lock:
                if path:
                    _bg_downloads[video_id] = {'status': 'done', 'path': path}
                else:
                    _bg_downloads[video_id] = {'status': 'failed', 'path': None}
        except Exception as e:
            print(f"[bg] Download error {video_id}: {str(e)[:200]}")
            with _bg_downloads_lock:
                _bg_downloads[video_id] = {'status': 'failed', 'path': None}

    t = threading.Thread(target=do_download, daemon=True)
    t.start()


def _wait_for_bg_download(video_id, timeout=45):
    start = time.time()
    while (time.time() - start) < timeout:
        cached = _get_cached_file(video_id)
        if cached and os.path.exists(cached):
            return cached

        with _bg_downloads_lock:
            info = _bg_downloads.get(video_id, {})
            if info.get('status') == 'done' and info.get('path'):
                return info['path']
            if info.get('status') == 'failed':
                return None

        time.sleep(0.3)
    return None


@app.route('/api/stream')
def stream():
    video_id = flask_request.args.get('v', '').strip()
    if not video_id:
        return jsonify({'error': 'Missing video ID'}), 400
    try:
        result = _get_stream_url(video_id)
        if result:
            _bg_download_file(video_id)

            safe_result = {k: v for k, v in result.items() if k != 'http_headers'}
            return jsonify({'status': 'ok', **safe_result})
        else:
            return jsonify({'error': 'Could not extract stream URL'}), 500
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500


def _get_mime(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext in ['.m4a', '.mp4']:
        return 'audio/mp4'
    elif ext == '.mp3':
        return 'audio/mpeg'
    elif ext == '.webm':
        return 'audio/webm'
    return 'audio/mp4'


def _serve_file_with_range(filepath, mime):
    file_size = os.path.getsize(filepath)
    range_header = flask_request.headers.get('Range')

    if range_header:
        try:
            range_val = range_header.replace('bytes=', '')
            parts = range_val.split('-')
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1

            with open(filepath, 'rb') as f:
                f.seek(start)
                data = f.read(length)

            resp = Response(
                data,
                status=206,
                mimetype=mime,
                headers={
                    'Content-Range': f'bytes {start}-{end}/{file_size}',
                    'Accept-Ranges': 'bytes',
                    'Content-Length': str(length),
                    'Content-Type': mime,
                }
            )
            return resp
        except Exception:
            pass

    with open(filepath, 'rb') as f:
        data = f.read()

    return Response(
        data,
        status=200,
        mimetype=mime,
        headers={
            'Accept-Ranges': 'bytes',
            'Content-Length': str(file_size),
            'Content-Type': mime,
        }
    )


@app.route('/api/proxy-stream', methods=['GET', 'HEAD'])
def proxy_stream():
    video_id = flask_request.args.get('v', '').strip()
    if not video_id:
        return jsonify({'error': 'Missing video ID'}), 400

    try:
        _cleanup_old_stream_files()

        cached_path = _get_cached_file(video_id)
        if not cached_path or not os.path.exists(cached_path):
            _bg_download_file(video_id)
            cached_path = _wait_for_bg_download(video_id, timeout=60)

        if not cached_path or not os.path.exists(cached_path):
            return jsonify({'error': 'Could not get audio'}), 500

        mime = _get_mime(cached_path)
        file_size = os.path.getsize(cached_path)

        if flask_request.method == 'HEAD':
            print(f"[proxy] HEAD ready {video_id} ({file_size} bytes)")
            return Response(
                '',
                status=200,
                mimetype=mime,
                headers={
                    'Accept-Ranges': 'bytes',
                    'Content-Length': str(file_size),
                    'Content-Type': mime,
                }
            )

        print(f"[proxy] Serving {video_id} ({file_size} bytes)")
        return _serve_file_with_range(cached_path, mime)

    except Exception as e:
        print(f"[proxy-stream] Error for {video_id}: {str(e)[:200]}")
        return jsonify({'error': 'Stream proxy failed'}), 500


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
        cached_path = _get_cached_file(video_id)
        if not cached_path or not os.path.exists(cached_path):
            cached_path = _download_stream_to_file(video_id)

        if cached_path and os.path.exists(cached_path):
            import subprocess
            mp3_out = os.path.join(session_dir, f"{video_id}.mp3")
            try:
                song_title = req_title or video_id
                if not req_title:
                    try:
                        opts = _get_ydl_opts(PLAYER_CLIENT_COMBOS[0])
                        opts['skip_download'] = True
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                            song_title = info.get('title', video_id)
                    except:
                        pass

                thumb_path = os.path.join(session_dir, f"{video_id}_thumb.jpg")
                thumb_ok = False
                try:
                    for thumb_url in [
                        f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
                    ]:
                        resp = req_lib.get(thumb_url, timeout=5)
                        if resp.status_code == 200 and len(resp.content) > 1000:
                            with open(thumb_path, 'wb') as tf:
                                tf.write(resp.content)
                            thumb_ok = True
                            break
                except:
                    pass

                ffmpeg_cmd = ['ffmpeg', '-y', '-i', cached_path]
                if thumb_ok:
                    ffmpeg_cmd += ['-i', thumb_path]
                ffmpeg_cmd += ['-codec:a', 'libmp3lame', '-b:a', '192k']
                if thumb_ok:
                    ffmpeg_cmd += ['-map', '0:a', '-map', '1:0', '-id3v2_version', '3',
                                   '-metadata:s:v', 'title=Album cover', '-metadata:s:v', 'comment=Cover (front)']
                else:
                    ffmpeg_cmd += ['-map', 'a']
                ffmpeg_cmd += [
                    '-metadata', f'title={song_title}',
                    '-metadata', f'artist=YouTube',
                    mp3_out
                ]
                result = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=120)

                if result.returncode == 0 and os.path.exists(mp3_out) and os.path.getsize(mp3_out) > 0:
                    filepath = mp3_out
                    safe_title = song_title.encode('ascii', 'ignore').decode('ascii').strip()
                    safe_title = re.sub(r'[<>:"/\\|?*]', '', safe_title).strip() or 'download'
                    filename = f"{safe_title}.mp3"
                    filesize = os.path.getsize(filepath)
                    print(f"[download] Fast convert from cache: {video_id} -> {filename} ({filesize} bytes)")

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
            except Exception as e:
                print(f"[download] Fast path failed for {video_id}, falling back: {str(e)[:100]}")

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
