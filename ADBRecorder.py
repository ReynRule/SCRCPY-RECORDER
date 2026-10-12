import sys
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

from PyQt6 import QtWidgets, QtCore

IS_WIN = os.name == 'nt'
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
CREATE_NEW_CONSOLE = subprocess.CREATE_NEW_CONSOLE if IS_WIN else 0

# 실행 파일 이름 (ensure_scrcpy/resolve_adb/resolve_scrcpy에서 사용)
ADB_NAME = 'adb.exe' if IS_WIN else 'adb'
SCRCPY_NAME = 'scrcpy.exe' if IS_WIN else 'scrcpy'

GRACEFUL_STOP_TIMEOUT = 20      # 초: Ctrl+C 후 파일을 마무리할 때까지 기다리는 시간
MIN_RUN_BEFORE_STOP = 2.0       # 초: scrcpy 초기화 전에 Ctrl+C 가 가면 파일이 안 생김


# --------------------------------------------------------------- 경로 처리
def app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def bundled_scrcpy_src():
    """exe 안에 내장된(또는 개발 중에는 스크립트 옆) scrcpy 폴더."""
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, 'scrcpy')
    return os.path.join(app_dir(), 'scrcpy')


def ensure_scrcpy():
    """내장 scrcpy를 영구 폴더에 풀어두고 그 경로를 돌려줍니다."""
    src = bundled_scrcpy_src()
    if not os.path.isdir(src):
        return None
    if not getattr(sys, 'frozen', False):
        return src

    version = 'unknown'
    try:
        with open(os.path.join(src, 'version.txt'), encoding='utf-8') as f:
            version = f.read().strip() or version
    except OSError:
        pass

    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    dst = os.path.join(base, 'ADBRecorder', 'scrcpy-' + version)
    if (os.path.exists(os.path.join(dst, SCRCPY_NAME)) and
            os.path.exists(os.path.join(dst, ADB_NAME))):
        return dst
    try:
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
    except OSError:
        return None
    return dst


# -------------------------------------------------------- scrcpy 정상 종료
def request_graceful_stop(proc):
    """scrcpy에 Ctrl+C를 보내 mp4/mkv 파일을 정상적으로 마무리하게 합니다."""
    if not IS_WIN:
        proc.send_signal(signal.SIGINT)
        return True

    import ctypes
    k32 = ctypes.windll.kernel32
    ok = False
    k32.FreeConsole()
    if k32.AttachConsole(proc.pid):
        try:
            # 이 프로세스 자신은 Ctrl+C를 무시하도록 하고 이벤트 전송
            k32.SetConsoleCtrlHandler(None, True)
            ok = bool(k32.GenerateConsoleCtrlEvent(0, 0))
            time.sleep(0.3)
        finally:
            k32.FreeConsole()
            k32.SetConsoleCtrlHandler(None, False)
    return ok


class ScreenRecorder(QtWidgets.QWidget):
    recording_finished = QtCore.pyqtSignal(str, str)   # (종류, 메시지)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('ADB 화면 녹화기 (scrcpy)')
        self.setFixedSize(460, 440)
        self.settings = QtCore.QSettings('MyCompany', 'ADBRecorder')
        self.recording = False
        self.stop_requested = False
        self.proc = None
        self.proc_started_at = 0.0
        self.rec_started_at = 0.0
        self._forced_kill = False
        self._scrcpy_help_cache = None
        self.scrcpy_dir = ensure_scrcpy()

        self.init_ui()
        self.load_settings()

        self.clock = QtCore.QTimer(self)
        self.clock.timeout.connect(self.update_clock)
        self.recording_finished.connect(self.on_recording_finished)

    # ------------------------------------------------------------------ UI
    def init_ui(self):
        layout = QtWidgets.QVBoxLayout()

        self.status_label = QtWidgets.QLabel('⏹️ 대기 중')
        self.status_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet('font-size: 16px;')
        layout.addWidget(self.status_label)

        # ADB 경로
        self.adb_path_edit = QtWidgets.QLineEdit()
        self.adb_path_edit.setPlaceholderText('ADB 경로 (비워두면 내장 adb 사용)')
        browse_btn = QtWidgets.QPushButton('ADB 선택')
        browse_btn.clicked.connect(self.browse_adb)
        adb_layout = QtWidgets.QHBoxLayout()
        adb_layout.addWidget(self.adb_path_edit)
        adb_layout.addWidget(browse_btn)
        layout.addLayout(adb_layout)

        # 디바이스
        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.setMinimumWidth(180)
        refresh_btn = QtWidgets.QPushButton('디바이스 새로고침')
        refresh_btn.clicked.connect(self.load_devices)
        device_layout = QtWidgets.QHBoxLayout()
        device_layout.addWidget(self.device_combo, 1)
        device_layout.addWidget(refresh_btn)
        layout.addLayout(device_layout)

        # 저장 폴더
        self.out_dir_edit = QtWidgets.QLineEdit()
        self.out_dir_edit.setPlaceholderText('저장 폴더')
        out_btn = QtWidgets.QPushButton('폴더 선택')
        out_btn.clicked.connect(self.browse_out_dir)
        out_layout = QtWidgets.QHBoxLayout()
        out_layout.addWidget(self.out_dir_edit)
        out_layout.addWidget(out_btn)
        layout.addLayout(out_layout)

        # 형식 / 해상도
        opt_layout = QtWidgets.QHBoxLayout()
        self.format_combo = QtWidgets.QComboBox()
        self.format_combo.addItems(['mp4', 'mkv'])
        self.format_combo.setToolTip(
            'mkv는 비정상 종료 시에도 영상 보존에 더 유리합니다.\n'
            'mp4는 정상 종료해야 파일이 완성됩니다.')
        self.size_combo = QtWidgets.QComboBox()
        self.size_combo.addItem('원본 해상도', 0)
        self.size_combo.addItem('최대 1920', 1920)
        self.size_combo.addItem('최대 1280', 1280)
        opt_layout.addWidget(QtWidgets.QLabel('형식'))
        opt_layout.addWidget(self.format_combo)
        opt_layout.addWidget(self.size_combo)
        opt_layout.addStretch()
        layout.addLayout(opt_layout)

        # 체크하면 scrcpy의 playback 오디오 캡처를 사용하고,
        # 해제하면 오디오 없이 영상만 녹화합니다.
        self.keep_device_audio_check = QtWidgets.QCheckBox('소리 녹화')
        self.keep_device_audio_check.setToolTip(
            '체크: 영상과 기기 내부 소리를 함께 녹화합니다.\n'
            '해제: 소리 없이 영상만 녹화합니다.')
        layout.addWidget(self.keep_device_audio_check)

        self.start_btn = QtWidgets.QPushButton('녹화 시작')
        self.start_btn.clicked.connect(self.start_recording)
        self.stop_btn = QtWidgets.QPushButton('녹화 중지')
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_recording)
        layout.addWidget(self.start_btn)
        layout.addWidget(self.stop_btn)

        layout.addStretch()
        self.setLayout(layout)

    # ------------------------------------------------------------ settings
    def load_settings(self):
        self.adb_path_edit.setText(self.settings.value('adb_path', ''))
        self.out_dir_edit.setText(self.settings.value('out_dir', app_dir()))
        fmt = self.settings.value('format', 'mp4')
        idx = self.format_combo.findText(fmt)
        if idx != -1:
            self.format_combo.setCurrentIndex(idx)
        try:
            max_size = int(self.settings.value('max_size', 0))
        except (TypeError, ValueError):
            max_size = 0
        idx = self.size_combo.findData(max_size)
        if idx != -1:
            self.size_combo.setCurrentIndex(idx)

        # 기본값은 소리 녹화 켜짐.
        self.keep_device_audio_check.setChecked(
            str(self.settings.value('record_audio', 'true')).lower() in ('1', 'true', 'yes'))

        saved_device = self.settings.value('device', '')
        self.load_devices()
        index = self.device_combo.findText(saved_device)
        if index != -1:
            self.device_combo.setCurrentIndex(index)

    def save_settings(self):
        self.settings.setValue('adb_path', self.adb_path_edit.text())
        self.settings.setValue('device', self.device_combo.currentText())
        self.settings.setValue('out_dir', self.out_dir_edit.text())
        self.settings.setValue('format', self.format_combo.currentText())
        self.settings.setValue('max_size', self.size_combo.currentData())
        self.settings.setValue('record_audio', self.keep_device_audio_check.isChecked())

    # --------------------------------------------------------------- paths
    def resolve_adb(self):
        """사용자가 지정한 adb, 없으면 내장 adb를 돌려줍니다."""
        custom = self.adb_path_edit.text().strip().strip('"')
        if custom and os.path.isfile(custom):
            return custom
        if self.scrcpy_dir:
            bundled = os.path.join(self.scrcpy_dir, ADB_NAME)
            if os.path.isfile(bundled):
                return bundled
        return None

    def resolve_scrcpy(self):
        if self.scrcpy_dir:
            path = os.path.join(self.scrcpy_dir, SCRCPY_NAME)
            if os.path.isfile(path):
                return path
        return None

    def scrcpy_help(self, scrcpy):
        """현재 내장 scrcpy가 지원하는 옵션을 확인해 오래된 버전의 옵션 오류를 방지합니다."""
        if self._scrcpy_help_cache is not None:
            return self._scrcpy_help_cache
        try:
            result = subprocess.run(
                [scrcpy, '--help'], capture_output=True, text=True,
                errors='replace', creationflags=CREATE_NO_WINDOW, timeout=10)
            self._scrcpy_help_cache = (result.stdout or '') + '\n' + (result.stderr or '')
        except (OSError, subprocess.SubprocessError):
            self._scrcpy_help_cache = ''
        return self._scrcpy_help_cache

    def browse_adb(self):
        file, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'ADB 선택', '', 'ADB (*.exe)')
        if file:
            self.adb_path_edit.setText(file)
            self.load_devices()

    def browse_out_dir(self):
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, '저장 폴더 선택', self.out_dir_edit.text())
        if directory:
            self.out_dir_edit.setText(directory)

    def load_devices(self):
        adb = self.resolve_adb()
        if not adb:
            return
        try:
            result = subprocess.run(
                [adb, 'devices'], capture_output=True, text=True,
                errors='replace', creationflags=CREATE_NO_WINDOW, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return
        self.device_combo.clear()
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == 'device':
                self.device_combo.addItem(parts[0])

    # ----------------------------------------------------------- recording
    def start_recording(self):
        if self.recording:
            return

        adb = self.resolve_adb()
        scrcpy = self.resolve_scrcpy()
        device = self.device_combo.currentText()
        if not adb or not device:
            QtWidgets.QMessageBox.warning(
                self, '오류', 'ADB 경로나 디바이스를 확인하세요.\n에뮬레이터가 실행 중인지 확인한 뒤 새로고침하세요.')
            return
        if not scrcpy:
            QtWidgets.QMessageBox.warning(
                self, '오류', '내장 scrcpy를 찾을 수 없습니다.\n프로그램 옆에 scrcpy 폴더가 있는지 확인하세요.')
            return

        help_text = self.scrcpy_help(scrcpy)
        has_help = bool(help_text.strip())
        has_audio_source = '--audio-source' in help_text
        has_no_audio = '--no-audio' in help_text

        record_audio = self.keep_device_audio_check.isChecked()
        if record_audio and not has_audio_source and has_help:
            QtWidgets.QMessageBox.warning(
                self, '옵션 미지원',
                '현재 scrcpy 버전은 --audio-source 옵션을 지원하지 않습니다.\n'
                'scrcpy 2.x 이상 버전을 사용하세요.')
            return
        if not record_audio and not has_no_audio and has_help:
            QtWidgets.QMessageBox.warning(
                self, '옵션 미지원',
                '현재 scrcpy 버전은 --no-audio 옵션을 지원하지 않습니다.')
            return

        out_dir = self.out_dir_edit.text().strip() or app_dir()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, '오류', f'저장 폴더를 만들 수 없습니다.\n{e}')
            return

        fmt = self.format_combo.currentText()
        # 초 단위 이름 충돌을 피합니다.
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
        out_path = os.path.join(out_dir, f'record_{timestamp}.{fmt}')
        log_path = os.path.join(out_dir, f'record_{timestamp}.log')

        cmd = [scrcpy, '-s', device,
               '--no-window',
               '--no-control',
               f'--record={out_path}',
               f'--record-format={fmt}']

        # 사용자가 테스트해 정상 동작을 확인한 방식과 동일하게 playback 소스를 사용합니다.
        # 디바이스 ID와 출력 파일명은 선택 기기/타임스탬프로 매번 동적으로 지정됩니다.
        if record_audio:
            cmd += ['--audio-source=playback','--audio-codec=aac']
            audio_mode = '기기 내부 소리 녹화 (playback)'
        else:
            cmd += ['--no-audio']
            audio_mode = '소리 녹화 끔 (영상만)'

        max_size = self.size_combo.currentData()
        if max_size:
            cmd += ['-m', str(max_size)]

        env = os.environ.copy()
        env['ADB'] = adb

        self.stop_requested = False
        self.recording = True
        self.proc = None
        self._forced_kill = False
        self.rec_started_at = time.time()
        self.status_label.setText('🔴 녹화 중... 00:00:00')
        self.status_label.setStyleSheet('color: red; font-size: 16px;')
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.keep_device_audio_check.setEnabled(False)
        self.clock.start(1000)

        threading.Thread(
            target=self.record_worker,
            args=(cmd, env, out_path, log_path, audio_mode),
            daemon=True).start()

    def record_worker(self, cmd, env, out_path, log_path, audio_mode):
        tail = deque(maxlen=35)
        try:
            kwargs = {}
            if IS_WIN:
                # Ctrl+C를 보낼 scrcpy 콘솔은 별도 생성하되 화면에는 표시하지 않습니다.
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0
                kwargs['startupinfo'] = startup
                kwargs['creationflags'] = CREATE_NEW_CONSOLE
            proc = subprocess.Popen(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, **kwargs)
            self.proc = proc
        except OSError as e:
            self.recording_finished.emit('error', f'scrcpy 실행 실패:\n{e}')
            return

        self.proc_started_at = time.time()

        def pump_output():
            try:
                with open(log_path, 'w', encoding='utf-8', errors='replace') as log_file:
                    for raw in iter(proc.stdout.readline, b''):
                        line = raw.decode('utf-8', errors='replace').rstrip()
                        if line:
                            tail.append(line)
                            log_file.write(line + '\n')
                            log_file.flush()
            except OSError as e:
                tail.append(f'로그 파일 저장 실패: {e}')

        reader = threading.Thread(target=pump_output, daemon=True)
        reader.start()
        return_code = proc.wait()
        reader.join(timeout=3)

        forced = self._forced_kill
        self._forced_kill = False
        exists = os.path.isfile(out_path) and os.path.getsize(out_path) > 0
        detail = '\n'.join(tail) or '(scrcpy 출력 없음)'

        if exists and return_code == 0:
            msg = (f'저장 완료:\n{out_path}\n\n'
                   f'오디오 방식: {audio_mode}\n'
                   f'실행 로그: {log_path}')
            if forced and out_path.lower().endswith('.mp4'):
                msg += ('\n\n※ scrcpy를 강제로 종료했습니다. MP4가 손상됐을 수 있습니다.\n'
                        '가능하면 다음 녹화부터 MKV 형식을 사용하세요.')
            self.recording_finished.emit('ok', msg)
        else:
            reason = f'scrcpy 종료 코드: {return_code}'
            self.recording_finished.emit(
                'error',
                f'녹화가 정상 완료되지 않았습니다.\n{reason}\n\n'
                f'로그 파일: {log_path}\n\n최근 출력:\n{detail}')

    def update_clock(self):
        if not self.recording or self.stop_requested:
            return
        sec = int(time.time() - self.rec_started_at)
        self.status_label.setText(
            f'🔴 녹화 중... {sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}')

    def stop_recording(self):
        if not self.recording or self.stop_requested:
            return
        self.stop_requested = True
        self.stop_btn.setEnabled(False)
        self.status_label.setText('💾 저장 중...')
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self):
        # scrcpy 시작 직후라면 프로세스가 생성될 때까지 기다립니다.
        deadline = time.time() + 15
        while self.proc is None:
            if not self.recording or time.time() >= deadline:
                return
            time.sleep(0.05)

        wait = MIN_RUN_BEFORE_STOP - (time.time() - self.proc_started_at)
        if wait > 0:
            time.sleep(wait)

        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            request_graceful_stop(proc)
        except Exception:
            pass
        try:
            proc.wait(timeout=GRACEFUL_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._forced_kill = True
            proc.kill()

    def on_recording_finished(self, kind, message):
        self.clock.stop()
        self.recording_end()
        if kind == 'ok':
            QtWidgets.QMessageBox.information(self, '완료', message)
        else:
            QtWidgets.QMessageBox.warning(self, '오류', message)

    def recording_end(self):
        self.recording = False
        self.stop_requested = False
        self.proc = None
        self.status_label.setText('⏹️ 대기 중')
        self.status_label.setStyleSheet('color: black; font-size: 16px;')
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.keep_device_audio_check.setEnabled(True)

    def closeEvent(self, event):
        if self.recording:
            self.stop_recording()
            deadline = time.time() + GRACEFUL_STOP_TIMEOUT + 5
            while self.recording and time.time() < deadline:
                QtWidgets.QApplication.processEvents()
                time.sleep(0.05)
        self.save_settings()
        event.accept()


if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    window = ScreenRecorder()
    window.show()
    sys.exit(app.exec())
