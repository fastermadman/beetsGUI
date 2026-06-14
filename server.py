#!/usr/bin/env python3
"""
BeetsGUI Server — lokal Flask API
Kør: python3 server.py
Stop: Ctrl+C

Serverer beetsgui.html på http://localhost:1312
og tilbyder /run?cmd=... til at eksekvere beet-kommandoer med live output.
"""
import os
import re
import sys
import subprocess
import threading
import time
from pathlib import Path

try:
    from flask import Flask, Response, request, send_from_directory, jsonify
except ImportError:
    print("Flask ikke fundet.")
    print("Installer: pip install flask   (eller: pip3 install flask)")
    sys.exit(1)

# ── Konfiguration ──────────────────────────────────────────────────────────────
PORT       = 1312
SCRIPT_DIR = Path(__file__).parent.resolve()
HTML_FILE  = 'beetsgui.html'

# Navn på Safari Web App (Arkiv → Tilføj til Dock)
# Ændr dette hvis du gav appen et andet navn
SAFARI_APP_NAME = 'BeetsGUI'

# Kommandoer der må eksekveres (sikkerhed)
ALLOWED_PREFIXES = ('beet ', 'beet\t', 'fd ', 'du ', 'for ')

# ── Flask-app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Hjælpefunktioner ───────────────────────────────────────────────────────────
def find_beet() -> str:
    """Find beet-executable. Checker Homebrew-stier først."""
    candidates = [
        '/opt/homebrew/bin/beet',       # Apple Silicon Homebrew
        '/usr/local/bin/beet',           # Intel Homebrew
        os.path.expanduser('~/.local/bin/beet'),
        os.path.expanduser('~/.local/pipx/venvs/beets/bin/beet'),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    try:
        r = subprocess.run(['which', 'beet'], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return 'beet'  # Fallback: håb at den er i PATH


def list_volumes() -> list:
    """Returner navne på monterede eksterne volumes (ikke Macintosh HD)."""
    skip = {'Macintosh HD', '.timemachine', 'Recovery', 'VM', 'Preboot', 'com.apple.TimeMachine.localsnapshots'}
    vols = []
    vol_path = Path('/Volumes')
    if vol_path.exists():
        for v in sorted(vol_path.iterdir()):
            if v.name not in skip and not v.name.startswith('.'):
                vols.append(v.name)
    return vols


def strip_ansi(text: str) -> str:
    """Fjern ANSI-escape-sekvenser fra tekst."""
    return re.sub(r'\x1b\[[0-9;]*[mGKHABCDJM]', '', text)


def get_config_path() -> str:
    """Find beets config-fil via 'beet config --path'."""
    try:
        r = subprocess.run(
            [find_beet(), 'config', '--path'],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return os.path.expanduser('~/.config/beets/config.yaml')



def open_app_when_ready():
    """Åbn Safari Web App (eller Safari) når serveren er klar."""
    time.sleep(1.2)

    # Prøv Safari Web App først (kræver Arkiv → Tilføj til Dock i Safari)
    result = subprocess.run(
        ['osascript', '-e', f'tell application "{SAFARI_APP_NAME}" to activate'],
        capture_output=True, timeout=5
    )
    if result.returncode != 0:
        # Fallback: åbn i Safari (ikke Zen, selv om Zen er standardbrowser)
        subprocess.run(
            ['open', '-a', 'Safari', f'http://localhost:{PORT}'],
            capture_output=True
        )


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route('/config', methods=['GET'])
def read_config():
    """Læs beets config.yaml."""
    path = get_config_path()
    try:
        content = Path(path).read_text() if Path(path).exists() else ''
        return jsonify({'ok': True, 'content': content, 'path': path})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/config', methods=['POST'])
def write_config():
    """Skriv beets config.yaml. Laver automatisk .bak-backup først."""
    import shutil
    data    = request.get_json()
    content = data.get('content', '')
    path    = Path(get_config_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, str(path) + '.bak')
        path.write_text(content)
        return jsonify({'ok': True, 'path': str(path)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500



@app.route('/playlists')
def list_playlists():
    """Find .m3u-playlister i en mappe — bruges til USB Mirror."""
    dir_path = os.path.expanduser(request.args.get('dir', '~/Playlister'))
    try:
        p = Path(dir_path)
        playlists = sorted([f.stem for f in p.rglob('*.m3u') if not f.name.startswith('.')]) if p.exists() else []
        return jsonify({'ok': True, 'playlists': playlists, 'dir': str(p)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/')
def index():
    return send_from_directory(str(SCRIPT_DIR), HTML_FILE)


@app.route('/status')
def status():
    """Server-status, beet-sti og monterede volumes."""
    return jsonify({
        'ok':      True,
        'beet':    find_beet(),
        'volumes': list_volumes(),
        'port':    PORT,
    })


@app.route('/run')
def run_cmd():
    """
    Eksekvér en kommando og stream output som Server-Sent Events (SSE).

    Parameter: ?cmd=beet import -A "/Volumes/Harddisk/Musik"

    SSE-linjer:
      data: <output-linje>\n\n
      data: __END__\n\n     ← signalerer at processen er færdig
    """
    cmd = request.args.get('cmd', '').strip()

    def sse(text: str) -> str:
        return f"data: {text}\n\n"

    def generate(cmd: str):
        # Validering
        if not cmd:
            yield sse("✗ Ingen kommando angivet")
            yield sse("__END__")
            return

        if not any(cmd.startswith(p) for p in ALLOWED_PREFIXES):
            yield sse(f"✗ Ikke tilladt: '{cmd}'")
            yield sse("  Kun beet-, fd- og du-kommandoer er tilladte.")
            yield sse("__END__")
            return

        # Erstat 'beet ' med fuld sti
        beet = find_beet()
        if cmd.startswith('beet ') or cmd.startswith('beet\t'):
            full_cmd = beet + cmd[4:]
        else:
            full_cmd = cmd

        yield sse(f"$ {cmd}")
        yield sse("")

        env = os.environ.copy()
        env['TERM']          = 'dumb'
        env['NO_COLOR']      = '1'
        env['BEETS_NO_COLOR'] = '1'

        try:
            proc = subprocess.Popen(
                full_cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                cwd=os.path.expanduser('~'),
            )

            for line in iter(proc.stdout.readline, ''):
                clean = strip_ansi(line.rstrip('\r\n'))
                yield sse(clean)

            proc.stdout.close()
            rc = proc.wait()
            yield sse("")
            yield sse("✓ Færdig" if rc == 0 else f"✗ Exit {rc}")

        except Exception as e:
            yield sse(f"FEJL: {e}")

        yield sse("__END__")

    return Response(
        generate(cmd),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection':        'keep-alive',
        }
    )


# ── Start ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    beet = find_beet()
    vols = list_volumes()

    print(f"\n{'─'*52}")
    print(f"  BeetsGUI Server  →  http://localhost:{PORT}")
    print(f"{'─'*52}")
    print(f"  beet:    {beet}")
    print(f"  volumes: {', '.join(vols) if vols else '(ingen monterede)'}")
    print(f"  html:    {SCRIPT_DIR / HTML_FILE}")
    print(f"{'─'*52}")
    print(f"  Stop: Ctrl+C\n")

    threading.Thread(target=open_app_when_ready, daemon=True).start()

    try:
        app.run(
            host='127.0.0.1',
            port=PORT,
            debug=False,
            threaded=True,
            use_reloader=False,
        )
    except KeyboardInterrupt:
        print("\nServer stoppet.")
