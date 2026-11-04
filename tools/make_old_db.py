"""База DryerCalc так, как её оставляет старая версия: журнал-образец, калибровка «Применить», партии с лабораторией,
пары влагомера. Из таких баз сделаны tests/data/dryer_v*.db — тест проверяет, что новая версия открывает их, не теряя
ни журнала, ни подобранной модели.

    python tools/make_old_db.py <папка исходников старой версии> <выходной файл .db>

Исходники старой версии: git worktree add /tmp/v070 <коммит> (или распакованный DryerCalc_vX_src.zip).
"""
import os, sys, json
src, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, src)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication([])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARN", a[1:3]) or QMessageBox.Ok)
from app import __version__
from app.storage import Storage
from app.gui import MainWindow
from app.calibration import read_journal
if os.path.exists(out):
    os.remove(out)
win = MainWindow(Storage(out))
rows = read_journal(os.path.join(src, "examples", "journal_sample_oct2026.csv"))[0]
win._append_rows(rows)
win._save_journal()
win._calibrate()
try:
    from app import worker
    worker.wait_all()
except ImportError:
    pass
for _ in range(30):
    app.processEvents()
win.tab_batches._from_journal()
win.tab_batches._fill_batches([dict(b, m_in=400, w_in=30, m_out=352, w_out=23.6, fuel=5000, fuel_price=7)
                               for b in win.tab_batches._batches()])
win.tab_batches._save_batches()
win.storage.set_list("meter_pairs", [{"reading": 15.0 + d, "ref": 15.0} for d in (0.6, 0.9, 0.8, 1.1, 0.7)])
win.close()
p = win.storage.load_params()
print(__version__, "calibrated", p.calibrated, "r_ref %.4f b_w %.4f eta %.4f" % (p.r_ref, p.b_w, p.eta), p.calib_info)
print("keys", sorted(k for (k,) in win.storage.conn.execute("SELECT key FROM settings")))
win.storage.close()
