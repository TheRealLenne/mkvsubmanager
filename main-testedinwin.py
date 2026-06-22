import sys, os, re, json, subprocess
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QHeaderView,
    QAbstractItemView, QFileDialog, QTableWidgetItem, QStackedWidget,
    QListWidget, QPlainTextEdit
)
from PySide6.QtCore import Qt, QThread, Signal
from qfluentwidgets import (
    TableWidget, PrimaryPushButton, PushButton, BodyLabel, TitleLabel,
    InfoBar, InfoBarPosition, MessageBox, setTheme, Theme,
    ProgressBar, IndeterminateProgressBar, Pivot
)

MKVMERGE = r"C:\Program Files\MKVToolNix\mkvmerge.exe" # Change this to the mkvmerge.exe file path
MKVEXTRACT = r"C:\Program Files\MKVToolNix\mkvextract.exe" # Change this to the mkvextract.exe file path
TESSERACT_DIR = r"C:\Users\User\AppData\Local\Programs\Tesseract-OCR" # Change this path to Tesseract OCR Directory

os.environ["PATH"] = TESSERACT_DIR + os.pathsep + os.environ.get("PATH", "")

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = str(Path(TESSERACT_DIR) / "tesseract.exe")
except ImportError:
    pass

try:
    from pgsrip import Sup, Options, pgsrip
    from babelfish import Language
    PGSRIP_AVAILABLE = True
except ImportError:
    PGSRIP_AVAILABLE = False

SCRIPT_DIR = Path(__file__).resolve().parent
ASS_TAG_RE = re.compile(r"\{.*?\}")


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

    srt_blocks, n = [], 0
    for ev in events:
        start = ass_time_to_srt(ev["Start"].strip())
        end = ass_time_to_srt(ev["End"].strip())
        clean = ASS_TAG_RE.sub("", ev.get("Text", ""))
        clean = clean.replace("\\N", "\n").replace("\\n", "\n").strip()
        if not clean:
            continue
        n += 1
        srt_blocks.append(f"{n}\n{start} --> {end}\n{clean}\n")

    srt_path.write_text("\n".join(srt_blocks), encoding="utf-8")


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
    out = subprocess.run([MKVMERGE, "-J", mkv_path], capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    return [t for t in data.get("tracks", []) if t["type"] == "subtitles"]


def remux_without_subtitles(mkv_path: str, out_path: Path):
    subprocess.run([MKVMERGE, "-o", str(out_path), "-S", mkv_path], check=True, capture_output=True, text=True)


def extract_subtitle_track(mkv_path: str, track: dict, out_dir: Path, base_name: str, used_names: set) -> Path:
    props = track.get("properties", {})
    codec_id = props.get("codec_id", "")
    lang = props.get("language", "und")
    tid = track["id"]

    if codec_id in ("S_TEXT/ASS", "S_TEXT/SSA"):
        raw_ext, conversion = ("ass" if codec_id == "S_TEXT/ASS" else "ssa"), "ass"
    elif codec_id == "S_TEXT/UTF8":
        raw_ext, conversion = "srt", None
    elif codec_id == "S_HDMV/PGS":
        raw_ext, conversion = "sup", "pgs"
    elif codec_id == "S_VOBSUB":
        raw_ext, conversion = "idx", None
    else:
        raw_ext, conversion = "srt", None

    raw_path = out_dir / f"_raw_{tid}.{raw_ext}"
    subprocess.run([MKVEXTRACT, mkv_path, "tracks", f"{tid}:{raw_path}"],
                    check=True, capture_output=True, text=True)

    if conversion == "ass":
        final_path = unique_path(out_dir / dedup_name(base_name, lang, "srt", tid, used_names))
        ass_to_srt(raw_path, final_path)
        raw_path.unlink(missing_ok=True)
        return final_path

    if conversion == "pgs":
        if PGSRIP_AVAILABLE:
            final_path = unique_path(out_dir / dedup_name(base_name, lang, "srt", tid, used_names))
            try:
                pgs_to_srt(raw_path, final_path, lang)
                raw_path.unlink(missing_ok=True)
                return final_path
            except Exception:
                pass  # OCR failed - fall through to raw bitmap below
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

        processed, failed = 0, []
        for i, (mkv_path, out_dir) in enumerate(jobs, 1):
            out_dir.mkdir(parents=True, exist_ok=True)
            out_mkv = out_dir / mkv_path.name
            self.log.emit(f"Processing {mkv_path.name}")
            try:
                tracks = get_subtitle_tracks(str(mkv_path))
                remux_without_subtitles(str(mkv_path), out_mkv)
                used_names = set()
                for t in tracks:
                    try:
                        extract_subtitle_track(str(mkv_path), t, out_dir, mkv_path.stem, used_names)
                    except Exception as e:
                        self.log.emit(f"   subtitle extract failed (track {t['id']}): {e}")
                processed += 1
                self.log.emit(f"   done -> {out_mkv}")
            except subprocess.CalledProcessError as e:
                msg = e.stderr.strip()
                failed.append(f"{mkv_path.name}: {msg}")
                self.log.emit(f"   failed: {msg}")
            except Exception as e:
                failed.append(f"{mkv_path.name}: {e}")
                self.log.emit(f"   failed: {e}")
            self.progress.emit(i, total)

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
        self.open_btn = PrimaryPushButton("Open MKV")
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
        self.extract_sel_btn = PushButton("Extract Selected")
        self.extract_all_btn = PushButton("Extract All")
        self.delete_sel_btn = PushButton("Delete Selected")
        self.delete_all_btn = PushButton("Delete All")
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
            self.notify_error(f"mkvmerge.exe not found at:\n{MKVMERGE}")
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

        cmd = [MKVMERGE, "-o", str(out_path)]
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
            "'<name>-Subripped' folder with subtitle-free mkvs and matching .srt files."
        ))

        top = QHBoxLayout()
        self.add_btn = PrimaryPushButton("Add Season Folder")
        self.add_btn.clicked.connect(self.add_folder)
        self.clear_btn = PushButton("Clear Queue")
        self.clear_btn.clicked.connect(self.clear_queue)
        top.addWidget(self.add_btn)
        top.addWidget(self.clear_btn)
        top.addStretch()
        root.addLayout(top)

        self.queue_list = QListWidget(self)
        self.queue_list.setMaximumHeight(120)
        root.addWidget(self.queue_list)

        self.start_btn = PrimaryPushButton("Start Processing")
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


# ---------- main window ----------

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MKV Subtitle Manager")
        self.resize(950, 680)
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

        self.add_sub_interface(self.single_iface, "single", "Single File")
        self.add_sub_interface(self.season_iface, "season", "Season Processing")

        self.stacked.currentChanged.connect(self.on_index_changed)
        self.stacked.setCurrentWidget(self.single_iface)
        self.pivot.setCurrentItem(self.single_iface.objectName())

        root.addWidget(self.pivot)
        root.addWidget(self.stacked, 1)

    def add_sub_interface(self, widget, key, text):
        widget.setObjectName(key)
        self.stacked.addWidget(widget)
        self.pivot.addItem(routeKey=key, text=text,
                           onClick=lambda: self.stacked.setCurrentWidget(widget))

    def on_index_changed(self, index):
        widget = self.stacked.widget(index)
        self.pivot.setCurrentItem(widget.objectName())


if __name__ == "__main__":
    setTheme(Theme.AUTO)
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
