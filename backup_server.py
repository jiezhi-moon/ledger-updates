#!/usr/bin/env python3
"""Local-only Safari ledger backup + per-file versioned GitHub updater (stdlib only)."""
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

HOST = '127.0.0.1'
PORT = 8765
SYSTEM_DIR = Path(__file__).resolve().parent
ROOT = SYSTEM_DIR.parent
HTML = ROOT / '記帳開票工具.html'
ICLOUD = Path.home() / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs'
BACKUP_DIR = ICLOUD / '記帳備份'
CONFIG = SYSTEM_DIR / 'update_config.json'
STATE = SYSTEM_DIR / '.update_state.json'
STAGE_DIR = SYSTEM_DIR / '.update-staging'
RESTART_EVENT = threading.Event()
staged_release = None
LOCK = threading.Lock()
UPDATE_LOCK = threading.Lock()
MAX_BODY = 20 * 1024 * 1024
MAX_UPDATE = 32 * 1024 * 1024
OCR_LOCK = threading.Lock()
OCR_SOURCE = SYSTEM_DIR / "macos_ocr.swift"
OCR_BINARY = SYSTEM_DIR / "macos_ocr"

CHECK_INTERVAL = 30 * 60
SESSION_TIMEOUT = 120
# An active tab keeps sending heartbeat (also when user is not typing).
SESSIONS = {}
last_active = time.monotonic()
update_status = {'status': 'disabled', 'message': '尚未設定 GitHub 更新網址'}

# Inject at response time, including for newly downloaded HTML, so Github assets
# remain unchanged and the SHA256 digest can be validated against the actual file.
HEARTBEAT_SCRIPT = r'''<script id="ledger-update-client">
(() => {
  const loadedVersion = __LOADED_VERSION__;
  const sid = (globalThis.crypto && crypto.randomUUID) ? crypto.randomUUID() :
    `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const sessionUrl = '/api/update/session';
  let closed = false;
  let lastEdit = Date.now();
  let refreshing = false;
  // Never refresh immediately after form input or while a field has focus.
  document.addEventListener('input', () => {lastEdit = Date.now();}, true);
  document.addEventListener('change', () => {lastEdit = Date.now();}, true);
  function send(action) {
    const payload = JSON.stringify({sid, action});
    if (action === 'close' && navigator.sendBeacon) {
      navigator.sendBeacon(sessionUrl, new Blob([payload], {type:'text/plain'}));
    } else {
      fetch(sessionUrl, {method:'POST', body:payload,
        headers:{'Content-Type':'text/plain'}, keepalive:true}).catch(() => {});
    }
  }
  function canRefresh() {
    if (document.visibilityState !== 'visible') return false;
    const focus = document.activeElement;
    if (focus && (focus.matches('input,textarea,select,[contenteditable]') || focus.isContentEditable)) return false;
    if (Date.now() - lastEdit < 120000) return false;
    if (window.__ledgerUpdateCanReload && !window.__ledgerUpdateCanReload()) return false;
    return true;
  }
  async function check() {
    if (closed || refreshing || !canRefresh()) return;
    try {
      const response = await fetch('/api/update/status', {cache:'no-store'});
      if (!response.ok) return;
      const body = await response.json();
      const version = body && body.installed_version;
      if (!version || version === loadedVersion) return;
      const values = v => v.split('.').map(Number);
      const newer = (v1,v2) => {
        const x=values(v1),y=values(v2);
        for(let i=0;i<3;i++){if(x[i]>y[i])return true;if(x[i]<y[i])return false;}
        return false;
      };
      if (!/^\d+\.\d+\.\d+$/.test(version) || !newer(version, loadedVersion)) return;
      if (!canRefresh()) return;
      refreshing = true;
      location.reload();
    } catch (_) { /* Offline or restarting: retry quietly. */ }
  }
  send('heartbeat');
  const heartbeat = setInterval(() => {if (!closed) send('heartbeat');}, 25000);
  const poll = setInterval(check, 15000);
  window.addEventListener('pagehide', () => {
    closed = true; clearInterval(heartbeat); clearInterval(poll); send('close');
  });
  window.addEventListener('pageshow', e => {
    if(e.persisted){closed=false;send('heartbeat');}
    check();
  });
  window.addEventListener('focus', check);
  document.addEventListener('visibilitychange', () => {if (!document.hidden) check();});
})();
</script>''' 



def atomic_bytes(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.ledger-write-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_json(path, data):
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def config():
    if not CONFIG.exists():
        return {}
    with CONFIG.open(encoding='utf-8') as f:
        return json.load(f)


def read_state():
    if not STATE.exists():
        return {'version': '1.0.0'}
    with STATE.open(encoding='utf-8') as f:
        return json.load(f)


def fetch(url, max_size):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('raw.githubusercontent.com', 'github.com'):
        raise ValueError('更新網址必須是 GitHub HTTPS 網址')
    req = urllib.request.Request(url, headers={'User-Agent': 'AuroraLedgerUpdater/1.0', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=15) as response:
        final = urlsplit(response.geturl())
        if final.scheme != 'https' or final.hostname not in ('raw.githubusercontent.com', 'github.com'):
            raise ValueError('更新下載重導向到不允許的網址')
        data = response.read(max_size + 1)
        if len(data) > max_size:
            raise ValueError('更新檔案超過大小限制')
        return data


def validate_update_url(url):
    if not isinstance(url, str):
        raise ValueError('缺少檔案下載網址')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('raw.githubusercontent.com', 'github.com') or parsed.username or parsed.password:
        raise ValueError('更新網址必須是 GitHub HTTPS 網址')
    return url


def update_target(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise ValueError('更新檔案路徑不合法')
    parts = name.split('/')
    if any(p in ('', '.', '..') or p.startswith('.') for p in parts):
        raise ValueError('更新路徑不可包含隱藏檔或上層目錄')
    # Only program files belong to a release; keep data and upgrade backups outside it.
    if parts[0] not in ('記帳開票工具.html', '啟動記帳工具.command', '使用說明.txt', '系統檔案'):
        raise ValueError('更新檔案必須位於程式目錄')
    target = ROOT.joinpath(*parts)
    for parent in [target, *target.parents]:
        if parent == ROOT:
            break
        if parent.is_symlink():
            raise ValueError('更新路徑不可包含符號連結')
    if not target.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('更新路徑超出程式目錄')
    return target


def valid_version(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value)


def validate_manifest(m):
    if not isinstance(m, dict) or not valid_version(m.get('version')):
        raise ValueError('version.json 的版本應為 x.y.z')
    files = m.get('files')
    if files is None:
        # Compatible with earlier HTML-only releases.
        files = [{'path': '記帳開票工具.html', 'version': m['version'], 'url': m.get('url'), 'sha256': m.get('sha256')}]
    if not isinstance(files, list) or not 1 <= len(files) <= 128:
        raise ValueError('files 應列出所有程式檔案（最多 128 個）')
    result, seen = [], set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('檔案資訊格式不正確')
        name, version, digest = item.get('path'), item.get('version'), item.get('sha256')
        update_target(name)
        if name.casefold() in seen:
            raise ValueError('更新清單有重複檔案')
        seen.add(name.casefold())
        if not valid_version(version):
            raise ValueError(f'{name} 缺少獨立版本號 x.y.z')
        if not isinstance(digest, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', digest):
            raise ValueError(f'{name} 缺少有效 SHA-256')
        executable = item.get('executable', False)
        if type(executable) is not bool:
            raise ValueError('executable 必須是布林值')
        result.append({'path': name, 'version': version, 'url': validate_update_url(item.get('url')), 'sha256': digest.lower(), 'executable': executable})
    return m['version'], result


def version_tuple(v):
    return tuple(int(x) for x in v.split('.'))


def check_update_content(item, data):
    if hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError(f"{item['path']} SHA-256 驗證失敗")
    suffix = Path(item['path']).suffix.lower()
    if suffix in ('.html', '.py', '.json'):
        text = data.decode('utf-8')
        if suffix == '.html' and ('</html>' not in text.lower() or '<body' not in text.lower()):
            raise ValueError('下載檔案不像完整的 HTML')
        if suffix == '.py':
            ast.parse(text, filename=item['path'])
        if suffix == '.json':
            json.loads(text)


def update_once():
    global update_status, staged_release
    with UPDATE_LOCK:
        try:
            manifest_url = config().get('manifest_url', '').strip()
            if not manifest_url:
                update_status = {'status': 'disabled', 'message': '尚未設定 GitHub 更新網址'}
                return
            manifest = json.loads(fetch(manifest_url, 256 * 1024).decode('utf-8'))
            version, files = validate_manifest(manifest)
            state = read_state()
            installed = state.get('version', '1.0.0')
            if version_tuple(version) < version_tuple(installed):
                update_status = {'status': 'current', 'version': installed}
                return
            known = state.get('files', {})
            pending, metadata, total = [], dict(known), 0
            STAGE_DIR.mkdir(exist_ok=True)
            for item in files:
                name = item['path']
                prior = known.get(name, {})
                prior_version = prior.get('version', '0.0.0')
                if version_tuple(item['version']) < version_tuple(prior_version):
                    continue  # Never downgrade a file independently.
                if item['version'] == prior_version and prior.get('sha256') not in (None, item['sha256']):
                    raise ValueError(f'{name} 內容已變更，發布時請提高該檔案版本號')
                target = update_target(name)
                current_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
                metadata[name] = {'version': item['version'], 'sha256': item['sha256']}
                mode_ok = target.exists() and bool(target.stat().st_mode & 0o111) == item['executable']
                if current_hash == item['sha256'] and mode_ok:
                    continue  # Includes an unchanged file with a newer declared version.
                candidate = STAGE_DIR / item['sha256']
                if current_hash == item['sha256']:
                    data = target.read_bytes()  # Only its executable flag needs repair.
                elif candidate.exists() and hashlib.sha256(candidate.read_bytes()).hexdigest() == item['sha256']:
                    data = candidate.read_bytes()
                else:
                    update_status = {'status': 'downloading', 'version': version, 'file': name}
                    data = fetch(item['url'], MAX_UPDATE)
                total += len(data)
                if total > 128 * 1024 * 1024:
                    raise ValueError('本次更新超過 128 MB')
                check_update_content(item, data)
                atomic_bytes(candidate, data)
                pending.append(item)
            staged_release = {'version': version, 'files': pending, 'metadata': metadata}
            if not pending and version == installed and known == metadata:
                update_status = {'status': 'current', 'version': installed}
                staged_release = None
                return
            atomic_json(STAGE_DIR / 'release.json', staged_release)
            update_status = {'status': 'ready', 'version': version, 'files': [i['path'] for i in pending], 'file_count': len(pending)}
            print(f'[更新] v{version} 已驗證，需更新 {len(pending)} 個檔案', flush=True)
        except Exception as exc:
            staged_release = None
            update_status = {'status': 'error', 'message': str(exc)}
            print('[更新] 檢查失敗，原版不受影響：', exc, flush=True)


def restore_staged_release():
    global staged_release, update_status
    cached = STAGE_DIR / 'release.json'
    if not cached.is_file():
        return
    try:
        release = json.loads(cached.read_text(encoding='utf-8'))
        version = release['version']
        if not valid_version(version) or version_tuple(version) < version_tuple(read_state().get('version', '1.0.0')):
            return
        if not release.get('files'):
            return
        _, items = validate_manifest({'version': version, 'files': release['files']})
        metadata = release['metadata']
        if not isinstance(metadata, dict):
            raise ValueError('暫存版本資訊無效')
        for name, info in metadata.items():
            update_target(name)
            if not isinstance(info, dict) or not valid_version(info.get('version')) or not re.fullmatch(r'[a-f0-9]{64}', info.get('sha256', '')):
                raise ValueError('暫存檔案版本資訊無效')
        for item in items:
            if metadata.get(item['path']) != {'version': item['version'], 'sha256': item['sha256']}:
                raise ValueError('暫存檔案版本資訊不一致')
            check_update_content(item, (STAGE_DIR / item['sha256']).read_bytes())
        staged_release = {'version': version, 'files': items, 'metadata': metadata}
        update_status = {'status': 'ready', 'version': version, 'file_count': len(items), 'files': [i['path'] for i in items]}
    except Exception as exc:
        staged_release = None
        update_status = {'status': 'error', 'message': '暫存更新無法還原：' + str(exc)}


def promote_when_idle(force=False):
    global update_status, staged_release
    # Serialize with backup and native OCR, so replacing the binary cannot interrupt OCR.
    with LOCK, UPDATE_LOCK, OCR_LOCK:
        now = time.monotonic()
        for sid, ts in list(SESSIONS.items()):
            if now - ts > SESSION_TIMEOUT:
                SESSIONS.pop(sid, None)
        if SESSIONS and not force:
            return
        if update_status.get('status') != 'ready' or staged_release is None:
            return
        release = staged_release
        version, items = release['version'], release['files']
        old_state = STATE.read_bytes() if STATE.exists() else None
        originals, changed = {}, []
        try:
            for item in items:
                check_update_content(item, (STAGE_DIR / item['sha256']).read_bytes())
                target = update_target(item['path'])
                originals[item['path']] = (target.read_bytes(), target.stat().st_mode & 0o777) if target.exists() else None
            backup = SYSTEM_DIR / '.update-backups' / (version + '_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
            backup.mkdir(parents=True)
            for name, original in originals.items():
                if original is not None:
                    dest = backup / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    atomic_bytes(dest, original[0])
                    dest.chmod(original[1])
            if old_state is not None:
                atomic_bytes(backup / 'previous-state.json', old_state)
            for item in items:
                target = update_target(item['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                changed.append(item['path'])
                atomic_bytes(target, (STAGE_DIR / item['sha256']).read_bytes())
                target.chmod(0o755 if item['executable'] else 0o644)
            atomic_json(STATE, {'version': version, 'installed_at': datetime.now().isoformat(), 'files': release['metadata']})
            (STAGE_DIR / 'release.json').unlink(missing_ok=True)
            restart = any(i['path'].endswith('.py') for i in items)
            update_status = {'status': 'installed', 'version': version, 'file_count': len(items), 'restart_required': restart}
            staged_release = None
            print(f'[更新] 已安裝 v{version}（{len(items)} 個檔案）' + ('，正在重新啟動服務' if restart else ''), flush=True)
            if restart:
                RESTART_EVENT.set()
        except Exception as exc:
            failures = []
            for name in reversed(changed):
                try:
                    target = update_target(name)
                    original = originals[name]
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        atomic_bytes(target, original[0])
                        target.chmod(original[1])
                except Exception as rollback_error:
                    failures.append(str(rollback_error))
            try:
                if old_state is None:
                    STATE.unlink(missing_ok=True)
                else:
                    atomic_bytes(STATE, old_state)
            except Exception as rollback_error:
                failures.append(str(rollback_error))
            staged_release = None
            update_status = {'status': 'error', 'message': str(exc) + ('；還原失敗：' + '；'.join(failures) if failures else '；已還原更新前檔案')}
            print('[更新] 安裝錯誤：', update_status['message'], flush=True)


def updater_loop():
    time.sleep(3)
    next_check = time.monotonic() + CHECK_INTERVAL if update_status.get('status') == 'ready' else 0
    while not RESTART_EVENT.is_set():
        if time.monotonic() >= next_check:
            update_once()
            next_check = time.monotonic() + CHECK_INTERVAL
        promote_when_idle()
        RESTART_EVENT.wait(5)


def recognize_native_image(data, regions=None, mode="text"):
    if sys.platform != 'darwin':
        raise RuntimeError('系統文字辨識需要 macOS，請在 Mac 上啟動記帳工具')
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('請傳送 PNG 圖片')
    with OCR_LOCK:
        if not OCR_BINARY.exists() or OCR_BINARY.stat().st_mtime < OCR_SOURCE.stat().st_mtime:
            try:
                compiled = subprocess.run(
                    ['/usr/bin/xcrun', 'swiftc', '-O', str(OCR_SOURCE), '-o', str(OCR_BINARY)],
                    capture_output=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError('無法準備系統辨識工具，請確認已安裝 Apple 命令列工具') from exc
            if compiled.returncode:
                raise RuntimeError('無法編譯系統辨識工具，請確認已安裝 Apple 命令列工具')
        with tempfile.TemporaryDirectory(prefix='ledger-ocr-') as tmp:
            image = Path(tmp) / 'input.png'
            image.write_bytes(data)
            args = [str(OCR_BINARY), str(image)]
            args.extend([json.dumps(regions or [[0, 1]]), mode])
            result = subprocess.run(args, capture_output=True, timeout=90)
            if result.returncode:
                detail = result.stderr.decode('utf-8', errors='replace')[:400]
                raise RuntimeError('系統文字辨識失敗：' + detail)
            payload = json.loads(result.stdout)
            if not isinstance(payload.get('words'), list):
                raise RuntimeError('系統辨識結果格式錯誤')
            return payload


class Handler(BaseHTTPRequestHandler):
    def respond(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/update/status':
            with LOCK:
                n = len(SESSIONS)
            self.respond(200, {'ok': True, 'update': dict(update_status), 'open_tabs': n, 'installed_version': read_state().get('version', '1.0.0')})
            return
        if path not in ('/', '/index.html'):
            self.respond(404, {'error': 'not found'})
            return
        body = HTML.read_bytes()
        marker = b'</body>'
        pos = body.lower().rfind(marker)
        if pos >= 0:
            body = body[:pos] + HEARTBEAT_SCRIPT.replace('__LOADED_VERSION__', json.dumps(read_state().get('version', '1.0.0'))).encode('utf-8') + body[pos:]
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global last_active
        path = urlsplit(self.path).path
        if path not in ('/api/backup', '/api/update/session', '/api/update/check', '/api/update/apply', '/api/ocr'):
            self.respond(404, {'ok': False, 'error': 'not found'})
            return
        origin = self.headers.get('Origin', '')
        if origin and origin != f'http://{HOST}:{PORT}':
            self.respond(403, {'ok': False, 'error': '拒絕跨網站請求'})
            return
        if path == '/api/ocr':
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'image/png':
                self.respond(415, {'ok': False, 'error': 'Content-Type 必須為 image/png'})
                return
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= MAX_BODY:
                    raise ValueError('圖片大小不合法或超過 20 MB')
                data = self.rfile.read(n)
                if len(data) != n:
                    raise ValueError('圖片傳輸不完整')
                regions = json.loads(self.headers.get('X-OCR-Regions', 'null'))
                if regions is not None:
                    if not isinstance(regions, list) or not 1 <= len(regions) <= 400:
                        raise ValueError('辨識範圍格式錯誤')
                    for region in regions:
                        if not isinstance(region, list) or len(region) != 2 or not all(type(v) in (int, float) for v in region) or not 0 <= region[0] < region[1] <= 1:
                            raise ValueError('辨識範圍格式錯誤')
                mode = self.headers.get("X-OCR-Mode", "text")
                if mode not in ("text", "number", "quantity"):
                    raise ValueError("辨識模式錯誤")
                self.respond(200, recognize_native_image(data, regions, mode))
            except ValueError as exc:
                self.respond(400, {'ok': False, 'error': str(exc)})
            except Exception as exc:
                self.respond(500, {'ok': False, 'error': str(exc)})
            return
        if path in ('/api/update/check', '/api/update/apply'):
            # User-triggered update check from the HTML UI. Same validation path as automatic updates.
            if self.headers.get('Content-Type','').split(';')[0].strip().lower() != 'text/plain':
                self.respond(415, {'ok': False, 'error': 'Content-Type 必須為 text/plain'})
                return
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if n < 0 or n > 1024:
                    raise ValueError('request too large')
                if n:
                    self.rfile.read(n)
                if path == '/api/update/check':
                    update_once()
                else:
                    promote_when_idle(force=True)
                self.respond(200, {'ok': True, 'update': dict(update_status), 'installed_version': read_state().get('version', '1.0.0')})
            except Exception as exc:
                self.respond(400, {'ok': False, 'error': str(exc)})
            return
        if path == '/api/update/session':
            if self.headers.get('Content-Type','').split(';')[0].strip().lower() != 'text/plain':
                self.respond(415, {'ok': False});return
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 1024:raise ValueError('invalid length')
                data = json.loads(self.rfile.read(n))
                sid, action = data.get('sid'), data.get('action')
                if not isinstance(sid, str) or len(sid) > 100 or action not in ('heartbeat', 'close'):
                    raise ValueError('invalid session')
                with LOCK:
                    last_active = time.monotonic()
                    if action == 'close': SESSIONS.pop(sid, None)
                    else: SESSIONS[sid] = last_active
                self.respond(200, {'ok': True})
            except Exception:
                self.respond(400, {'ok': False})
            return
        if self.headers.get('Content-Type','').split(';')[0].strip().lower() != 'application/json':
            self.respond(415, {'ok': False, 'error': 'Content-Type 必須為 application/json'})
            return
        try:
            n = int(self.headers.get('Content-Length', '0'))
            if not (0 < n <= MAX_BODY):raise ValueError('備份大小不合法或超過 20 MB')
            payload = json.loads(self.rfile.read(n))
            if not isinstance(payload, dict) or payload.get('format') != 'invoice-ledger-backup' or not isinstance(payload.get('projects'), list):
                raise ValueError('備份資料格式不正確')
            if not ICLOUD.is_dir():raise RuntimeError('找不到 iCloud Drive；請先在 Mac 啟用 iCloud Drive')
            BACKUP_DIR.mkdir(exist_ok=True)
            manual_path = None
            with LOCK:
                now = datetime.now()
                atomic_json(BACKUP_DIR / '最新備份.json', payload)
                day = BACKUP_DIR / '每日備份'
                day.mkdir(exist_ok=True)
                daily = day / (now.strftime('%Y-%m-%d') + '.json')
                if not daily.exists():atomic_json(daily, payload)
                if payload.get('manual') is True:
                    manual_dir = BACKUP_DIR / '手動備份'
                    manual_dir.mkdir(exist_ok=True)
                    snapshot = manual_dir / (now.strftime('%Y-%m-%d_%H%M%S_%f') + '.json')
                    atomic_json(snapshot, payload)
                    manual_path = str(snapshot)
            self.respond(200, {'ok': True, 'path': str(BACKUP_DIR), 'manual_path': manual_path})
        except Exception as exc:
            self.respond(400, {'ok': False, 'error': str(exc)})


if __name__ == '__main__':
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = False  # Finish responses before replacing the running Python process.
    restore_staged_release()
    threading.Thread(target=updater_loop, daemon=True, name='github-update',).start()
    print('記帳工具已啟動： http://127.0.0.1:8765', flush=True)
    print('備份資料夾：', BACKUP_DIR, flush=True)
    print('更新狀態： http://127.0.0.1:8765/api/update/status', flush=True)
    print('保持此視窗開啟；按 Control+C 停止。', flush=True)
    if not os.environ.pop('LEDGER_SERVICE_RESTART', ''):
        webbrowser.open('http://127.0.0.1:8765')
    def restart_watcher():
        RESTART_EVENT.wait()
        server.shutdown()
    threading.Thread(target=restart_watcher, daemon=True, name='service-restart').start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    if RESTART_EVENT.is_set():
        os.environ['LEDGER_SERVICE_RESTART'] = '1'
        os.execv(sys.executable, [sys.executable, str(SYSTEM_DIR / 'backup_server.py')])
