import sys, os, re, json, subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QHeaderView,
    QAbstractItemView, QFileDialog, QTableWidget, QTableWidgetItem,
    QStackedWidget, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QLabel, QLineEdit, QComboBox, QSpinBox, QProgressBar,
    QMessageBox, QFrame
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont

# ---------- persistent config ----------

CONFIG_DIR = Path.home() / ".config" / "mkvsubtitlemanager"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "theme": "dark",  # light, dark
    "mkvmerge_path": "mkvmerge",
    "mkvextract_path": "mkvextract",
    "tesseract_dir": "",
    "max_workers": 4,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(data)
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


CONFIG = load_config()


def apply_tesseract_path():
    tdir = CONFIG.get("tesseract_dir", "")
    if tdir:
        os.environ["PATH"] = tdir + os.pathsep + os.environ.get("PATH", "")
        try:
            import pytesseract
            exe = Path(tdir) / "tesseract"
            if exe.exists():
                pytesseract.pytesseract.tesseract_cmd = str(exe)
        except ImportError:
            pass


apply_tesseract_path()

try:
    from pgsrip import Sup, Options, pgsrip
    from babelfish import Language
    PGSRIP_AVAILABLE = True
except ImportError:
    PGSRIP_AVAILABLE = False

SCRIPT_DIR = Path(__file__).resolve().parent
ASS_TAG_RE = re.compile(r"\{.*?\}")

CODEC_MAP = {
    "S_TEXT/ASS": ("ass", "ass"),
    "S_TEXT/SSA": ("ssa", "ass"),
    "S_TEXT/UTF8": ("srt", None),
    "S_TEXT/WEBVTT": ("vtt", "vtt"),
    "S_HDMV/PGS": ("sup", "pgs"),
    "S_VOBSUB": ("idx", None),
    "S_DVBSUB": ("sub", None),
}


# ---------- GNOME-ish stylesheets ----------

DARK_QSS = """
QWidget { background-color: #242424; color: #e3e3e3; font-family: "Inter", "Cantarell", "Noto Sans", sans-serif; font-size: 10.5pt; }
QFrame#Sidebar { background-color: #1e1e1e; border-right: 1px solid #333333; }
QFrame#HeaderBar { background-color: #2a2a2a; border-bottom: 1px solid #333333; }
QListWidget#NavList { background: transparent; border: none; padding: 8px; }
QListWidget#NavList::item { padding: 10px 12px; border-radius: 8px; margin-bottom: 2px; }
QListWidget#NavList::item:selected { background-color: #3584e4; color: white; }
QListWidget#NavList::item:hover:!selected { background-color: #333333; }
QPushButton { background-color: #333333; color: #e3e3e3; border: 1px solid #3d3d3d; border-radius: 8px; padding: 8px 16px; }
QPushButton:hover { background-color: #3a3a3a; }
QPushButton:pressed { background-color: #2d2d2d; }
QPushButton:disabled { color: #777777; background-color: #2a2a2a; }
QPushButton#Accent { background-color: #3584e4; border: none; color: white; font-weight: 600; }
QPushButton#Accent:hover { background-color: #4090ec; }
QPushButton#Danger { background-color: #c01c28; border: none; color: white; }
QPushButton#Danger:hover { background-color: #d52f3c; }
QTableWidget, QListWidget, QPlainTextEdit, QLineEdit {
  background-color: #2a2a2a; border: 1px solid #3a3a3a; border-radius: 8px; color: #e3e3e3;
  gridline-color: #3a3a3a; selection-background-color: #3584e4; selection-color: white;
}
QHeaderView::section { background-color: #2f2f2f; color: #cfcfcf; border: none; padding: 6px; }
QProgressBar { background-color: #2a2a2a; border: 1px solid #3a3a3a; border-radius: 8px; text-align: center; color: #e3e3e3; }
QProgressBar::chunk { background-color: #3584e4; border-radius: 8px; }
QComboBox, QSpinBox { background-color: #2a2a2a; border: 1px solid #3a3a3a; border-radius: 8px; padding: 6px; color: #e3e3e3; }
QLabel#Title { font-size: 16pt; font-weight: 700; color: #ffffff; }
QLabel#Subtitle { color: #a8a8a8; }
QFrame#Card { background-color: #2a2a2a; border: 1px solid #3a3a3a; border-radius: 12px; }
"""

LIGHT_QSS = """
QWidget { background-color: #fafafa; color: #1e1e1e; font-family: "Inter", "Cantarell", "Noto Sans", sans-serif; font-size: 10.5pt; }
QFrame#Sidebar { background-color: #f0f0f0; border-right: 1px solid #dcdcdc; }
QFrame#HeaderBar { background-color: #f5f5f5; border-bottom: 1px solid #dcdcdc; }
QListWidget#NavList { background: transparent; border: none; padding: 8px; }
QListWidget#NavList::item { padding: 10px 12px; border-radius: 8px; margin-bottom: 2px; }
QListWidget#NavList::item:selected { background-color: #3584e4; color: white; }
QListWidget#NavList::item:hover:!selected { background-color: #e8e8e8; }
QPushButton { background-color: #ffffff; color: #1e1e1e; border: 1px solid #d0d0d0; border-radius: 8px; padding: 8px 16px; }
QPushButton:hover { background-color: #f0f0f0; }
QPushButton:pressed { background-color: #e5e5e5; }
QPushButton:disabled { color: #aaaaaa; background-color: #f5f5f5; }
QPushButton#Accent { background-color: #3584e4; border: none; color: white; font-weight: 600; }
QPushButton#Accent:hover { background-color: #2e6fc4; }
QPushButton#Danger { background-color: #e01b24; border: none; color: white; }
QPushButton#Danger:hover { background-color: #c4151d; }
QTableWidget, QListWidget, QPlainTextEdit, QLineEdit {
  background-color: #ffffff; border: 1px solid #dcdcdc; border-radius: 8px; color: #1e1e1e;
  gridline-color: #e0e0e0; selection-background-color: #3584e4; selection-color: white;
}
QHeaderView::section { background-color: #f0f0f0; color: #444444; border: none; padding: 6px; }
QProgressBar { background-color: #eeeeee; border: 1px solid #dcdcdc; border-radius: 8px; text-align: center; color: #1e1e1e; }
QProgressBar::chunk { background-color: #3584e4; border-radius: 8px; }
QComboBox, QSpinBox { background-color: #ffffff; border: 1px solid #dcdcdc; border-radius: 8px; padding: 6px; color: #1e1e1e; }
QLabel#Title { font-size: 16pt; font-weight: 700; color: #1e1e1e; }
QLabel#Subtitle { color: #6a6a6a; }
QFrame#Card { background-color: #ffffff; border: 1px solid #dcdcdc; border-radius: 12px; }
"""


def apply_theme(app: QApplication, name: str):
    app.setStyleSheet(DARK_QSS if name == "dark" else LIGHT_QSS)


# ---------- shared helpers (identical logic to Windows version) ----------

def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def dedup_name(base: str, lang: str, ext: str, tid: int, used_names: set) -> str:
    name = f"{base}.{lang}.{ext}"
    if name in used_names:
        name = f"{base}.{lang}.{tid}.{ext}"
    used_names.add(name)
    return name


def ass_time_to_srt(t: str) -> str:
    h, m, s = t.split(":")
    sec, cs = s.split(".")
    ms = int(cs) * 10
    return f"{int(h):02d}:{int(m):02d}:{int(sec):02d},{ms:03d}"


def ass_to_srt(ass_path: Path, srt_path: Path):
    text = ass_path.read_text(encoding="utf-8", errors="replace")
    fmt_fields, events, in_events = None, [], False
    for line in text.splitlines():
        if line.strip().lower() == "[events]":
            in_events = True
            continue
        if not in_events:
            continue
        if line.startswith("Format:"):
            fmt_fields = [f.strip() for f in line[len("Format:"):].split(",")]
            continue
        if line.startswith("Dialogue:") and fmt_fields:
            rest = line[len("Dialogue:"):].strip()
            parts = rest.split(",", len(fmt_fields) - 1)
            events.append(dict(zip(fmt_fields, parts)))

    blocks, n = [], 0
    for ev in events:
        start = ass_time_to_srt(ev["Start"].strip())
        end = ass_time_to_srt(ev["End"].strip())
        clean = ASS_TAG_RE.sub("", ev.get("Text", ""))
        clean = clean.replace("\\N", "\n").replace("\\n", "\n").strip()
        if not clean:
            continue
        n += 1
        blocks.append(f"{n}\n{start} --> {end}\n{clean}\n")
    srt_path.write_text("\n".join(blocks), encoding="utf-8")


def vtt_to_srt(vtt_path: Path, srt_path: Path):
    lines = vtt_path.read_text(encoding="utf-8", errors="replace").splitlines()
    blocks, n = [], 0
    timing, cue_lines = None, []

    def flush():
        nonlocal n
        if timing and cue_lines:
            text_block = "\n".join(cue_lines).strip()
            if text_block:
                n += 1
                blocks.append(f"{n}\n{timing}\n{text_block}\n")

    for line in lines:
        if "-->" in line:
            flush()
            cue_lines = []
            start_raw, end_raw = line.split("-->")
            start = start_raw.strip().split(" ")[0].replace(".", ",")
            end = end_raw.strip().split(" ")[0].replace(".", ",")
            timing = f"{start} --> {end}"
        elif line.strip() == "" or line.strip().upper() == "WEBVTT":
            continue
        else:
            cue_lines.append(line)
    flush()
    srt_path.write_text("\n".join(blocks), encoding="utf-8")


def resolve_language(lang_code: str):
    try:
        return Language(lang_code)
    except Exception:
        pass
    try:
        return Language.fromalpha3b(lang_code)
    except Exception:
        pass
    return Language("eng")


def pgs_to_srt(sup_path: Path, srt_path: Path, lang_code: str):
    language = resolve_language(lang_code)
    media = Sup(sup_path)
    options = Options(languages={language}, overwrite=True)
    pgsrip.rip(media, options)
    produced = sup_path.with_suffix(".srt")
    if not produced.exists():
        raise RuntimeError("pgsrip produced no output")
    produced.replace(srt_path)


def get_subtitle_tracks(mkv_path: str) -> list:
    out = subprocess.run([CONFIG["mkvmerge_path"], "-J", mkv_path],
                          capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    return [t for t in data.get("tracks", []) if t["type"] == "subtitles"]


def remux_without_subtitles(mkv_path: str, out_path: Path):
    subprocess.run([CONFIG["mkvmerge_path"], "-o", str(out_path), "-S", mkv_path],
                    check=True, capture_output=True, text=True)


def extract_subtitle_track(mkv_path: str, track: dict, out_dir: Path, base_name: str, used_names: set) -> Path:
    props = track.get("properties", {})
    codec_id = props.get("codec_id", "")
    lang = props.get("language", "und")
    tid = track["id"]

    raw_ext, conversion = CODEC_MAP.get(codec_id, ("srt", None))
    raw_path = out_dir / f"_raw_{tid}.{raw_ext}"
    subprocess.run([CONFIG["mkvextract_path"], mkv_path, "tracks", f"{tid}:{raw_path}"],
                    check=True, capture_output=True, text=True)

    if conversion == "ass":
        final_path = unique_path(out_dir / dedup_name(base_name, lang, "srt", tid, used_names))
        ass_to_srt(raw_path, final_path)
        raw_path.unlink(missing_ok=True)
        return final_path

    if conversion == "vtt":
        final_path = unique_path(out_dir / dedup_name(base_name, lang, "srt", tid, used_names))
        try:
            vtt_to_srt(raw_path, final_path)
            raw_path.unlink(missing_ok=True)
        except Exception:
            final_path = unique_path(out_dir / dedup_name(base_name, lang, "vtt", tid, used_names))
            raw_path.replace(final_path)
        return final_path

    if conversion == "pgs":
        if PGSRIP_AVAILABLE:
            final_path = unique_path(out_dir / dedup_name(base_name, lang, "srt", tid, used_names))
            try:
                pgs_to_srt(raw_path, final_path, lang)
                raw_path.unlink(missing_ok=True)
                return final_path
            except Exception:
                pass
        final_path = unique_path(out_dir / dedup_name(base_name, lang, "sup", tid, used_names))
        raw_path.replace(final_path)
        return final_path

    final_path = unique_path(out_dir / dedup_name(base_name, lang, raw_ext, tid, used_names))
    raw_path.replace(final_path)
    return final_path


# ---------- background workers ----------

class ExtractWorker(QThread):
    progress = Signal(int, int)
    finished_ok = Signal(int, list)

    def __init__(self, mkv_path, tracks, ids, out_dir):
        super().__init__()
        self.mkv_path = mkv_path
        self.tracks = tracks
        self.ids = ids
        self.out_dir = out_dir

    def run(self):
        base = Path(self.mkv_path).stem
        used_names = set()
        done, failed = 0, []
        total = len(self.ids)
        for i, tid in enumerate(self.ids, 1):
            track = next(t for t in self.tracks if t["id"] == tid)
            try:
                extract_subtitle_track(self.mkv_path, track, self.out_dir, base, used_names)
                done += 1
            except subprocess.CalledProcessError as e:
                failed.append(f"track {tid}: {e.stderr.strip()}")
            except Exception as e:
                failed.append(f"track {tid}: {e}")
            self.progress.emit(i, total)
        self.finished_ok.emit(done, failed)


class DeleteWorker(QThread):
    finished_ok = Signal(bool, str)

    def __init__(self, cmd, out_path):
        super().__init__()
        self.cmd = cmd
        self.out_path = out_path

    def run(self):
        try:
            subprocess.run(self.cmd, check=True, capture_output=True, text=True)
            self.finished_ok.emit(True, str(self.out_path))
        except subprocess.CalledProcessError as e:
            self.finished_ok.emit(False, e.stderr)


class SeasonWorker(QThread):
    progress = Signal(int, int)
    log = Signal(str)
    finished_ok = Signal(int, list)

    def __init__(self, season_folders: list):
        super().__init__()
        self.season_folders = season_folders

    def _process_episode(self, mkv_path: Path, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        out_mkv = out_dir / mkv_path.name
        try:
            tracks = get_subtitle_tracks(str(mkv_path))
            remux_without_subtitles(str(mkv_path), out_mkv)
            used_names = set()
            for t in tracks:
                try:
                    extract_subtitle_track(str(mkv_path), t, out_dir, mkv_path.stem, used_names)
                except Exception as e:
                    self.log.emit(f"   subtitle extract failed for {mkv_path.name} (track {t['id']}): {e}")
            return True, ""
        except subprocess.CalledProcessError as e:
            return False, e.stderr.strip()
        except Exception as e:
            return False, str(e)

    def run(self):
        jobs = []
        for season_folder in self.season_folders:
            output_root = season_folder.parent / f"{season_folder.name}-Subripped"
            for mkv_path in sorted(season_folder.rglob("*.mkv")):
                rel_dir = mkv_path.parent.relative_to(season_folder)
                jobs.append((mkv_path, output_root / rel_dir))

        total = len(jobs)
        if total == 0:
            self.log.emit("No .mkv files found in selected folder(s).")
            self.finished_ok.emit(0, [])
            return

        processed, failed, done_count = 0, [], 0
        max_workers = max(1, int(CONFIG.get("max_workers", 4)))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self._process_episode, mkv_path, out_dir): mkv_path
                for mkv_path, out_dir in jobs
            }
            for future in as_completed(future_map):
                mkv_path = future_map[future]
                done_count += 1
                try:
                    ok, msg = future.result()
                    if ok:
                        processed += 1
                        self.log.emit(f"[{done_count}/{total}] done: {mkv_path.name}")
                    else:
                        failed.append(f"{mkv_path.name}: {msg}")
                        self.log.emit(f"[{done_count}/{total}] failed: {mkv_path.name} - {msg}")
                except Exception as e:
                    failed.append(f"{mkv_path.name}: {e}")
                    self.log.emit(f"[{done_count}/{total}] failed: {mkv_path.name} - {e}")
                self.progress.emit(done_count, total)

        self.finished_ok.emit(processed, failed)


# ---------- toast-style notification ----------

def notify(parent, kind: str, title: str, message: str):
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    icon = {"success": QMessageBox.Information, "error": QMessageBox.Critical,
            "info": QMessageBox.Information}.get(kind, QMessageBox.Information)
    box.setIcon(icon)
    box.exec()


# ---------- single file page ----------

class SingleFilePage(QWidget):
    def __init__(self):
        super().__init__()
        self.mkv_path = None
        self.tracks = []
        self.worker = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(14)

        root.addWidget(self._heading("Single File", "Inspect, extract, or strip subtitles from one MKV."))

        top = QHBoxLayout()
        self.open_btn = QPushButton("Open MKV…")
        self.open_btn.setObjectName("Accent")
        self.open_btn.clicked.connect(self.open_file)
        self.file_label = QLabel("No file loaded")
        self.file_label.setObjectName("Subtitle")
        top.addWidget(self.open_btn)
        top.addWidget(self.file_label)
        top.addStretch()
        root.addLayout(top)

        self.table = QTableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["ID", "Language", "Codec", "Track Name"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.extract_sel_btn = QPushButton("Extract Selected")
        self.extract_all_btn = QPushButton("Extract All")
        self.delete_sel_btn = QPushButton("Delete Selected")
        self.delete_sel_btn.setObjectName("Danger")
        self.delete_all_btn = QPushButton("Delete All")
        self.delete_all_btn.setObjectName("Danger")
        for b in (self.extract_sel_btn, self.extract_all_btn, self.delete_sel_btn, self.delete_all_btn):
            btns.addWidget(b)
        btns.addStretch()
        root.addLayout(btns)

        self.progress = QProgressBar(self)
        self.progress.hide()
        root.addWidget(self.progress)

        self.extract_sel_btn.clicked.connect(lambda: self.start_extract(self.selected_ids()))
        self.extract_all_btn.clicked.connect(lambda: self.start_extract([t["id"] for t in self.tracks]))
        self.delete_sel_btn.clicked.connect(lambda: self.start_delete(self.selected_ids()))
        self.delete_all_btn.clicked.connect(lambda: self.start_delete([t["id"] for t in self.tracks]))

    def _heading(self, title, subtitle):
        frame = QFrame()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        t = QLabel(title); t.setObjectName("Title")
        s = QLabel(subtitle); s.setObjectName("Subtitle")
        layout.addWidget(t); layout.addWidget(s)
        return frame

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open MKV", str(Path.home()), "MKV files (*.mkv)")
        if not path:
            return
        self.mkv_path = path
        self.file_label.setText(os.path.basename(path))
        self.load_tracks()

    def load_tracks(self):
        self.table.setRowCount(0)
        try:
            self.tracks = get_subtitle_tracks(self.mkv_path)
        except FileNotFoundError:
            notify(self, "error", "Missing tool",
                   f"mkvmerge not found at '{CONFIG['mkvmerge_path']}'. Set the correct path in Settings.")
            return
        except subprocess.CalledProcessError as e:
            notify(self, "error", "Error", e.stderr)
            return

        self.table.setRowCount(len(self.tracks))
        for row, t in enumerate(self.tracks):
            props = t.get("properties", {})
            self.table.setItem(row, 0, QTableWidgetItem(str(t["id"])))
            self.table.setItem(row, 1, QTableWidgetItem(props.get("language", "und")))
            self.table.setItem(row, 2, QTableWidgetItem(t.get("codec", "")))
            self.table.setItem(row, 3, QTableWidgetItem(props.get("track_name", "")))

        if not self.tracks:
            notify(self, "info", "No subtitles", "This file has no subtitle tracks.")

    def selected_ids(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        return [self.tracks[r]["id"] for r in rows]

    def set_busy(self, busy: bool):
        for b in (self.open_btn, self.extract_sel_btn, self.extract_all_btn,
                  self.delete_sel_btn, self.delete_all_btn):
            b.setEnabled(not busy)

    def start_extract(self, ids):
        if not self.mkv_path or not ids:
            if self.mkv_path:
                notify(self, "info", "No selection", "Select at least one subtitle track first.")
            return
        self.set_busy(True)
        self.progress.setRange(0, len(ids))
        self.progress.setValue(0)
        self.progress.show()
        self.worker = ExtractWorker(self.mkv_path, self.tracks, ids, SCRIPT_DIR)
        self.worker.progress.connect(lambda done, total: self.progress.setValue(done))
        self.worker.finished_ok.connect(self.on_extract_finished)
        self.worker.start()

    def on_extract_finished(self, done, failed):
        self.progress.hide()
        self.set_busy(False)
        if failed:
            notify(self, "error", "Finished with errors",
                   f"{done} extracted, {len(failed)} failed:\n" + "\n".join(failed))
        else:
            notify(self, "success", "Done", f"Extracted {done} track(s) to {SCRIPT_DIR}")

    def start_delete(self, remove_ids):
        if not self.mkv_path or not remove_ids:
            if self.mkv_path:
                notify(self, "info", "No selection", "Select at least one subtitle track first.")
            return

        confirm = QMessageBox.question(
            self, "Confirm",
            f"Remux the file without {len(remove_ids)} subtitle track(s)?\n"
            "This creates a new file next to the script — original stays untouched.",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        keep_ids = [str(t["id"]) for t in self.tracks if t["id"] not in remove_ids]
        base = Path(self.mkv_path).stem
        out_path = unique_path(SCRIPT_DIR / f"{base}.nosubs.mkv")

        cmd = [CONFIG["mkvmerge_path"], "-o", str(out_path)]
        cmd += ["-s", ",".join(keep_ids)] if keep_ids else ["-S"]
        cmd.append(self.mkv_path)

        self.set_busy(True)
        self.progress.setRange(0, 0)
        self.progress.show()

        self.worker = DeleteWorker(cmd, out_path)
        self.worker.finished_ok.connect(self.on_delete_finished)
        self.worker.start()

    def on_delete_finished(self, success, message):
        self.progress.hide()
        self.progress.setRange(0, 1)
        self.set_busy(False)
        if success:
            notify(self, "success", "Done", f"Saved to {Path(message).name}")
        else:
            notify(self, "error", "Remux failed", message)


# ---------- season processing page ----------

class SeasonPage(QWidget):
    def __init__(self):
        super().__init__()
        self.queue = []
        self.worker = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(14)

        root.addWidget(self._heading(
            "Season Processing",
            f"Mirrors season folders into '<name>-Subripped' copies. Runs {CONFIG.get('max_workers', 4)} episodes in parallel."
        ))

        top = QHBoxLayout()
        self.add_btn = QPushButton("Add Season Folder…")
        self.add_btn.setObjectName("Accent")
        self.add_btn.clicked.connect(self.add_folder)
        self.clear_btn = QPushButton("Clear Queue")
        top.addWidget(self.add_btn)
        top.addWidget(self.clear_btn)
        top.addStretch()
        root.addLayout(top)
        self.clear_btn.clicked.connect(self.clear_queue)

        self.queue_list = QListWidget(self)
        self.queue_list.setMaximumHeight(120)
        root.addWidget(self.queue_list)

        self.start_btn = QPushButton("Start Processing")
        self.start_btn.setObjectName("Accent")
        self.start_btn.clicked.connect(self.start_processing)
        root.addWidget(self.start_btn)

        self.progress = QProgressBar(self)
        self.progress.hide()
        root.addWidget(self.progress)

        self.log_box = QPlainTextEdit(self)
        self.log_box.setReadOnly(True)
        root.addWidget(self.log_box, 1)

    def _heading(self, title, subtitle):
        frame = QFrame()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        t = QLabel(title); t.setObjectName("Title")
        s = QLabel(subtitle); s.setObjectName("Subtitle")
        layout.addWidget(t); layout.addWidget(s)
        return frame

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select Season Folder", str(Path.home()))
        if not path:
            return
        p = Path(path)
        if p not in self.queue:
            self.queue.append(p)
            self.queue_list.addItem(str(p))

    def clear_queue(self):
        self.queue.clear()
        self.queue_list.clear()

    def set_busy(self, busy: bool):
        for b in (self.add_btn, self.clear_btn, self.start_btn):
            b.setEnabled(not busy)

    def start_processing(self):
        if not self.queue:
            notify(self, "info", "Info", "Add at least one season folder first.")
            return

        self.log_box.clear()
        self.set_busy(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.show()

        self.worker = SeasonWorker(list(self.queue))
        self.worker.progress.connect(lambda done, total: (
            self.progress.setRange(0, total), self.progress.setValue(done)
        ))
        self.worker.log.connect(self.log_box.appendPlainText)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.start()

    def on_finished(self, processed, failed):
        self.progress.hide()
        self.set_busy(False)
        if failed:
            notify(self, "error", "Done with errors",
                   f"{processed} processed, {len(failed)} failed — see log.")
        else:
            notify(self, "success", "Done", f"Processed {processed} episode(s).")


# ---------- settings page ----------

class SettingsPage(QWidget):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self._build_ui()
        self._load_into_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(18)

        title = QLabel("Settings"); title.setObjectName("Title")
        root.addWidget(title)

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("App theme"))
        self.theme_combo = QComboBox(self)
        self.theme_combo.addItems(["Dark", "Light"])
        self.theme_combo.currentTextChanged.connect(self.on_theme_changed)
        theme_row.addStretch()
        theme_row.addWidget(self.theme_combo)
        root.addLayout(theme_row)

        self.mkvmerge_edit = self._path_row(root, "mkvmerge path", self.browse_mkvmerge)
        self.mkvextract_edit = self._path_row(root, "mkvextract path", self.browse_mkvextract)
        self.tesseract_edit = self._path_row(root, "Tesseract-OCR folder (optional)", self.browse_tesseract)

        workers_row = QHBoxLayout()
        workers_row.addWidget(QLabel("Parallel jobs (season processing)"))
        self.workers_spin = QSpinBox(self)
        self.workers_spin.setRange(1, 16)
        workers_row.addStretch()
        workers_row.addWidget(self.workers_spin)
        root.addLayout(workers_row)

        root.addStretch()

        self.save_btn = QPushButton("Save Settings")
        self.save_btn.setObjectName("Accent")
        self.save_btn.clicked.connect(self.save_all)
        save_row = QHBoxLayout()
        save_row.addWidget(self.save_btn)
        save_row.addStretch()
        root.addLayout(save_row)

    def _path_row(self, root, label_text, browse_fn):
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        edit = QLineEdit(self)
        edit.setMinimumWidth(380)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(lambda: browse_fn(edit))
        row.addWidget(edit, 1)
        row.addWidget(browse_btn)
        root.addLayout(row)
        return edit

    def browse_mkvmerge(self, edit):
        path, _ = QFileDialog.getOpenFileName(self, "Select mkvmerge binary", "/usr/bin")
        if path:
            edit.setText(path)

    def browse_mkvextract(self, edit):
        path, _ = QFileDialog.getOpenFileName(self, "Select mkvextract binary", "/usr/bin")
        if path:
            edit.setText(path)

    def browse_tesseract(self, edit):
        path = QFileDialog.getExistingDirectory(self, "Select Tesseract-OCR folder", "/usr")
        if path:
            edit.setText(path)

    def _load_into_ui(self):
        self.theme_combo.setCurrentText(CONFIG.get("theme", "dark").capitalize())
        self.mkvmerge_edit.setText(CONFIG.get("mkvmerge_path", ""))
        self.mkvextract_edit.setText(CONFIG.get("mkvextract_path", ""))
        self.tesseract_edit.setText(CONFIG.get("tesseract_dir", ""))
        self.workers_spin.setValue(int(CONFIG.get("max_workers", 4)))

    def on_theme_changed(self, text):
        CONFIG["theme"] = text.lower()
        apply_theme(self.app, text.lower())
        save_config(CONFIG)

    def save_all(self):
        CONFIG["mkvmerge_path"] = self.mkvmerge_edit.text().strip() or "mkvmerge"
        CONFIG["mkvextract_path"] = self.mkvextract_edit.text().strip() or "mkvextract"
        CONFIG["tesseract_dir"] = self.tesseract_edit.text().strip()
        CONFIG["max_workers"] = self.workers_spin.value()
        apply_tesseract_path()
        save_config(CONFIG)
        notify(self, "success", "Saved", "Settings saved.")


# ---------- main window ----------

class MainWindow(QWidget):
    def __init__(self, app: QApplication):
        super().__init__()
        self.setWindowTitle("MKV Subtitle Manager")
        self.resize(1000, 700)
        self._build_ui(app)

    def _build_ui(self, app):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(220)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(0, 16, 0, 16)

        brand = QLabel("🎬  Subtitle Manager")
        brand.setStyleSheet("font-weight: 700; font-size: 11pt; padding: 0 16px 12px 16px;")
        side_layout.addWidget(brand)

        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.addItem(QListWidgetItem("Single File"))
        self.nav.addItem(QListWidgetItem("Season Processing"))
        self.nav.addItem(QListWidgetItem("Settings"))
        self.nav.currentRowChanged.connect(self.on_nav_changed)
        side_layout.addWidget(self.nav, 1)

        root.addWidget(sidebar)

        self.stacked = QStackedWidget()
        self.single_page = SingleFilePage()
        self.season_page = SeasonPage()
        self.settings_page = SettingsPage(app)
        self.stacked.addWidget(self.single_page)
        self.stacked.addWidget(self.season_page)
        self.stacked.addWidget(self.settings_page)
        root.addWidget(self.stacked, 1)

        self.nav.setCurrentRow(0)

    def on_nav_changed(self, index):
        self.stacked.setCurrentIndex(index)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    apply_theme(app, CONFIG.get("theme", "dark"))
    w = MainWindow(app)
    w.show()
    sys.exit(app.exec())
