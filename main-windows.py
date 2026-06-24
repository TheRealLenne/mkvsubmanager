import sys, os, re, json, subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QHeaderView,
    QAbstractItemView, QFileDialog, QTableWidgetItem, QStackedWidget,
    QListWidget, QPlainTextEdit
)
from PySide6.QtCore import Qt, QThread, Signal
from qfluentwidgets import (
    TableWidget, PrimaryPushButton, PushButton, BodyLabel, TitleLabel,
    InfoBar, InfoBarPosition, MessageBox, setTheme, Theme,
    ProgressBar, IndeterminateProgressBar, Pivot, ComboBox, LineEdit, SpinBox,
    FluentIcon as FIF
)

# ---------- persistent config (saved to AppData) ----------

APPDATA_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "MKVSubtitleManager"
APPDATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = APPDATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "theme": "auto",  # light, dark, auto
    "mkvmerge_path": r"C:\Program Files\MKVToolNix\mkvmerge.exe",
    "mkvextract_path": r"C:\Program Files\MKVToolNix\mkvextract.exe",
    "tesseract_dir": r"C:\Program Files\Tesseract-OCR",
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
            exe = Path(tdir) / "tesseract.exe"
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


def apply_theme(name: str):
    mapping = {"light": Theme.LIGHT, "dark": Theme.DARK, "auto": Theme.AUTO}
    setTheme(mapping.get(name, Theme.AUTO))


SCRIPT_DIR = Path(__file__).resolve().parent
ASS_TAG_RE = re.compile(r"\{.*?\}")

# codec_id -> (raw_extension, conversion_kind)
# conversion_kind: None = keep as-is, "ass" = ASS/SSA->SRT, "vtt" = WebVTT->SRT, "pgs" = OCR->SRT
CODEC_MAP = {
    "S_TEXT/ASS": ("ass", "ass"),
    "S_TEXT/SSA": ("ssa", "ass"),
    "S_TEXT/UTF8": ("srt", None),
    "S_TEXT/WEBVTT": ("vtt", "vtt"),
    "S_HDMV/PGS": ("sup", "pgs"),
    "S_VOBSUB": ("idx", None),
    "S_DVBSUB": ("sub", None),
}


# ---------- shared helpers ----------

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
    """Basic WebVTT -> SRT conversion (timestamps + cue text, strips cue settings)."""
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
                pass  # OCR failed - fall through to raw bitmap
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
    """Processes episodes in parallel using a thread pool (mkvmerge/mkvextract are
    external processes, so running several at once scales nicely with CPU cores)."""
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


# ---------- single file tab ----------

class SingleFileInterface(QWidget):
    def __init__(self):
        super().__init__()
        self.mkv_path = None
        self.tracks = []
        self.worker = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        top = QHBoxLayout()
        self.open_btn = PrimaryPushButton(FIF.VIDEO, "Open MKV")
        self.open_btn.clicked.connect(self.open_file)
        self.file_label = BodyLabel("No file loaded")
        top.addWidget(self.open_btn)
        top.addWidget(self.file_label)
        top.addStretch()
        root.addLayout(top)

        self.table = TableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["ID", "Language", "Codec", "Track Name"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.extract_sel_btn = PushButton(FIF.DOWNLOAD, "Extract Selected")
        self.extract_all_btn = PushButton(FIF.DOWNLOAD, "Extract All")
        self.delete_sel_btn = PushButton(FIF.DELETE, "Delete Selected")
        self.delete_all_btn = PushButton(FIF.DELETE, "Delete All")
        for b in (self.extract_sel_btn, self.extract_all_btn, self.delete_sel_btn, self.delete_all_btn):
            btns.addWidget(b)
        btns.addStretch()
        root.addLayout(btns)

        self.det_progress = ProgressBar(self)
        self.det_progress.hide()
        root.addWidget(self.det_progress)

        self.ind_progress = IndeterminateProgressBar(self)
        self.ind_progress.hide()
        root.addWidget(self.ind_progress)

        self.extract_sel_btn.clicked.connect(lambda: self.start_extract(self.selected_ids()))
        self.extract_all_btn.clicked.connect(lambda: self.start_extract([t["id"] for t in self.tracks]))
        self.delete_sel_btn.clicked.connect(lambda: self.start_delete(self.selected_ids()))
        self.delete_all_btn.clicked.connect(lambda: self.start_delete([t["id"] for t in self.tracks]))

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open MKV", "", "MKV files (*.mkv)")
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
            self.notify_error(f"mkvmerge.exe not found at:\n{CONFIG['mkvmerge_path']}\n\nSet the correct path in the Settings tab.")
            return
        except subprocess.CalledProcessError as e:
            self.notify_error(e.stderr)
            return

        self.table.setRowCount(len(self.tracks))
        for row, t in enumerate(self.tracks):
            props = t.get("properties", {})
            self.table.setItem(row, 0, QTableWidgetItem(str(t["id"])))
            self.table.setItem(row, 1, QTableWidgetItem(props.get("language", "und")))
            self.table.setItem(row, 2, QTableWidgetItem(t.get("codec", "")))
            self.table.setItem(row, 3, QTableWidgetItem(props.get("track_name", "")))

        if not self.tracks:
            self.notify_info("This file has no subtitle tracks.")

    def selected_ids(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        return [self.tracks[r]["id"] for r in rows]

    def set_busy(self, busy: bool):
        for b in (self.open_btn, self.extract_sel_btn, self.extract_all_btn,
                  self.delete_sel_btn, self.delete_all_btn):
            b.setEnabled(not busy)

    def start_extract(self, ids):
        if not self.mkv_path:
            return
        if not ids:
            self.notify_info("Select at least one subtitle track first.")
            return

        self.set_busy(True)
        self.det_progress.setRange(0, len(ids))
        self.det_progress.setValue(0)
        self.det_progress.show()

        self.worker = ExtractWorker(self.mkv_path, self.tracks, ids, SCRIPT_DIR)
        self.worker.progress.connect(lambda done, total: self.det_progress.setValue(done))
        self.worker.finished_ok.connect(self.on_extract_finished)
        self.worker.start()

    def on_extract_finished(self, done, failed):
        self.det_progress.hide()
        self.set_busy(False)
        if failed:
            self.notify_error(f"{done} extracted, {len(failed)} failed:\n" + "\n".join(failed))
        else:
            self.notify_success(f"Extracted {done} track(s) to {SCRIPT_DIR}")

    def start_delete(self, remove_ids):
        if not self.mkv_path:
            return
        if not remove_ids:
            self.notify_info("Select at least one subtitle track first.")
            return

        box = MessageBox(
            "Confirm",
            f"Remux the file without {len(remove_ids)} subtitle track(s)?\n"
            "This creates a new file next to the script — original stays untouched.",
            self
        )
        if not box.exec():
            return

        keep_ids = [str(t["id"]) for t in self.tracks if t["id"] not in remove_ids]
        base = Path(self.mkv_path).stem
        out_path = unique_path(SCRIPT_DIR / f"{base}.nosubs.mkv")

        cmd = [CONFIG["mkvmerge_path"], "-o", str(out_path)]
        cmd += ["-s", ",".join(keep_ids)] if keep_ids else ["-S"]
        cmd.append(self.mkv_path)

        self.set_busy(True)
        self.ind_progress.show()
        self.ind_progress.start()

        self.worker = DeleteWorker(cmd, out_path)
        self.worker.finished_ok.connect(self.on_delete_finished)
        self.worker.start()

    def on_delete_finished(self, success, message):
        self.ind_progress.stop()
        self.ind_progress.hide()
        self.set_busy(False)
        if success:
            self.notify_success(f"Saved to {Path(message).name}")
        else:
            self.notify_error(message)

    def notify_success(self, msg):
        InfoBar.success("Done", msg, parent=self, position=InfoBarPosition.TOP, duration=4000)

    def notify_error(self, msg):
        InfoBar.error("Error", msg, parent=self, position=InfoBarPosition.TOP, duration=6000)

    def notify_info(self, msg):
        InfoBar.info("Info", msg, parent=self, position=InfoBarPosition.TOP, duration=3000)


# ---------- season processing tab ----------

class SeasonInterface(QWidget):
    def __init__(self):
        super().__init__()
        self.queue = []
        self.worker = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        root.addWidget(BodyLabel(
            "Add one or more season folders. Each gets mirrored into a sibling "
            "'<name>-Subripped' folder with subtitle-free mkvs and matching .srt files.\n"
            f"Episodes process in parallel ({CONFIG.get('max_workers', 4)} at a time — change in Settings)."
        ))

        top = QHBoxLayout()
        self.add_btn = PrimaryPushButton(FIF.FOLDER_ADD, "Add Season Folder")
        self.add_btn.clicked.connect(self.add_folder)
        self.clear_btn = PushButton(FIF.DELETE, "Clear Queue")
        self.clear_btn.clicked.connect(self.clear_queue)
        top.addWidget(self.add_btn)
        top.addWidget(self.clear_btn)
        top.addStretch()
        root.addLayout(top)

        self.queue_list = QListWidget(self)
        self.queue_list.setMaximumHeight(120)
        root.addWidget(self.queue_list)

        self.start_btn = PrimaryPushButton(FIF.PLAY, "Start Processing")
        self.start_btn.clicked.connect(self.start_processing)
        root.addWidget(self.start_btn)

        self.det_progress = ProgressBar(self)
        self.det_progress.hide()
        root.addWidget(self.det_progress)

        self.log_box = QPlainTextEdit(self)
        self.log_box.setReadOnly(True)
        root.addWidget(self.log_box, 1)

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select Season Folder")
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
            InfoBar.info("Info", "Add at least one season folder first.",
                         parent=self, position=InfoBarPosition.TOP, duration=3000)
            return

        self.log_box.clear()
        self.set_busy(True)
        self.det_progress.setRange(0, 1)
        self.det_progress.setValue(0)
        self.det_progress.show()

        self.worker = SeasonWorker(list(self.queue))
        self.worker.progress.connect(lambda done, total: (
            self.det_progress.setRange(0, total), self.det_progress.setValue(done)
        ))
        self.worker.log.connect(self.log_box.appendPlainText)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.start()

    def on_finished(self, processed, failed):
        self.det_progress.hide()
        self.set_busy(False)
        if failed:
            InfoBar.error("Done with errors", f"{processed} processed, {len(failed)} failed — see log.",
                          parent=self, position=InfoBarPosition.TOP, duration=6000)
        else:
            InfoBar.success("Done", f"Processed {processed} episode(s).",
                            parent=self, position=InfoBarPosition.TOP, duration=4000)


# ---------- settings tab ----------

class SettingsInterface(QWidget):
    def __init__(self):
        super().__init__()
        self._build_ui()
        self._load_into_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(18)

        root.addWidget(TitleLabel("Settings"))

        # theme_row = QHBoxLayout()
        # theme_row.addWidget(BodyLabel("App theme"))
        # self.theme_combo = ComboBox(self)
        # self.theme_combo.addItems(["Light", "Dark", "Auto"])
        # self.theme_combo.currentTextChanged.connect(self.on_theme_changed)
        # theme_row.addStretch()
        # theme_row.addWidget(self.theme_combo)
        # root.addLayout(theme_row)

        self.mkvmerge_edit = self._path_row(root, "mkvmerge.exe path", self.browse_mkvmerge)
        self.mkvextract_edit = self._path_row(root, "mkvextract.exe path", self.browse_mkvextract)
        self.tesseract_edit = self._path_row(root, "Tesseract-OCR folder", self.browse_tesseract)

        workers_row = QHBoxLayout()
        workers_row.addWidget(BodyLabel("Parallel jobs (season processing)"))
        self.workers_spin = SpinBox(self)
        self.workers_spin.setRange(1, 16)
        workers_row.addStretch()
        workers_row.addWidget(self.workers_spin)
        root.addLayout(workers_row)

        root.addStretch()

        save_row = QHBoxLayout()
        self.save_btn = PrimaryPushButton(FIF.SAVE, "Save Settings")
        self.save_btn.clicked.connect(self.save_all)
        save_row.addWidget(self.save_btn)
        save_row.addStretch()
        root.addLayout(save_row)

    def _path_row(self, root, label_text, browse_fn):
        row = QHBoxLayout()
        row.addWidget(BodyLabel(label_text))
        edit = LineEdit(self)
        edit.setMinimumWidth(380)
        browse_btn = PushButton(FIF.FOLDER, "Browse")
        browse_btn.clicked.connect(lambda: browse_fn(edit))
        row.addWidget(edit, 1)
        row.addWidget(browse_btn)
        root.addLayout(row)
        return edit

    def browse_mkvmerge(self, edit):
        path, _ = QFileDialog.getOpenFileName(self, "Select mkvmerge.exe", "", "Executable (*.exe)")
        if path:
            edit.setText(path)

    def browse_mkvextract(self, edit):
        path, _ = QFileDialog.getOpenFileName(self, "Select mkvextract.exe", "", "Executable (*.exe)")
        if path:
            edit.setText(path)

    def browse_tesseract(self, edit):
        path = QFileDialog.getExistingDirectory(self, "Select Tesseract-OCR folder")
        if path:
            edit.setText(path)

    def _load_into_ui(self):
        # self.theme_combo.setCurrentText(CONFIG.get("theme", "auto").capitalize())
        self.mkvmerge_edit.setText(CONFIG.get("mkvmerge_path", ""))
        self.mkvextract_edit.setText(CONFIG.get("mkvextract_path", ""))
        self.tesseract_edit.setText(CONFIG.get("tesseract_dir", ""))
        self.workers_spin.setValue(int(CONFIG.get("max_workers", 4)))

    def on_theme_changed(self, text):
        CONFIG["theme"] = text.lower()
        apply_theme(text.lower())
        save_config(CONFIG)

    def save_all(self):
        CONFIG["mkvmerge_path"] = self.mkvmerge_edit.text().strip()
        CONFIG["mkvextract_path"] = self.mkvextract_edit.text().strip()
        CONFIG["tesseract_dir"] = self.tesseract_edit.text().strip()
        CONFIG["max_workers"] = self.workers_spin.value()
        apply_tesseract_path()
        save_config(CONFIG)
        InfoBar.success("Saved", "Settings saved.", parent=self, position=InfoBarPosition.TOP, duration=3000)


# ---------- main window ----------

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MKV Subtitle Manager")
        self.resize(950, 700)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        root.addWidget(TitleLabel("MKV Subtitle Manager"))

        self.pivot = Pivot(self)
        self.stacked = QStackedWidget(self)

        self.single_iface = SingleFileInterface()
        self.season_iface = SeasonInterface()
        self.settings_iface = SettingsInterface()

        self.add_sub_interface(self.single_iface, "single", "Single File", FIF.VIDEO)
        self.add_sub_interface(self.season_iface, "season", "Season Processing", FIF.FOLDER)
        self.add_sub_interface(self.settings_iface, "settings", "Settings", FIF.SETTING)

        self.stacked.currentChanged.connect(self.on_index_changed)
        self.stacked.setCurrentWidget(self.single_iface)
        self.pivot.setCurrentItem(self.single_iface.objectName())

        root.addWidget(self.pivot)
        root.addWidget(self.stacked, 1)

    def add_sub_interface(self, widget, key, text, icon=None):
        widget.setObjectName(key)
        self.stacked.addWidget(widget)
        self.pivot.addItem(routeKey=key, text=text, icon=icon,
                           onClick=lambda: self.stacked.setCurrentWidget(widget))

    def on_index_changed(self, index):
        widget = self.stacked.widget(index)
        self.pivot.setCurrentItem(widget.objectName())


if __name__ == "__main__":
    app = QApplication(sys.argv)
    apply_theme(CONFIG.get("theme", "auto"))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
