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
        self.setFixedSize(460, 475)
        self.settings = QtCore.QSettings('MyCompany', 'ADBRecorder')
        self.recording = False
        self.paused = False
        self.stop_requested = False
        self.proc = None
        self.proc_started_at = 0.0
        self.rec_started_at = 0.0
        self._active_elapsed = 0.0
        self._segment_started_at = 0.0
        self._pause_intervals = []
        self._pause_started_at = None
        self._timeline_start = 0.0
        self._last_display_second = -1
        self._segment_end_reason = None
        self._segment_index = 0
        self._segments = []
        self._segment_logs = []
        self._base_out_path = None
        self._base_log_stem = None
        self._current_segment_path = None
        self._current_segment_log = None
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
        self.pause_btn = QtWidgets.QPushButton('일시 정지')
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self.toggle_pause_resume)
        self.stop_btn = QtWidgets.QPushButton('녹화 종료 및 저장')
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_recording)
        control_layout = QtWidgets.QHBoxLayout()
        control_layout.addWidget(self.pause_btn)
        control_layout.addWidget(self.stop_btn)
        layout.addWidget(self.start_btn)
        layout.addLayout(control_layout)

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
                [adb, 'devices', '-l'],
                capture_output=True,
                text=True,
                errors='replace',
                creationflags=CREATE_NO_WINDOW,
                timeout=30
            )

            if result.returncode != 0:
                return

        except (OSError, subprocess.SubprocessError):
            return

        previous_device = self.device_combo.currentText()

        self.device_combo.clear()

        for line in result.stdout.splitlines()[1:]:
            parts = line.split()

            if len(parts) >= 2 and parts[1] == 'device':
                serial = parts[0]
                details = ' '.join(parts[2:])
                label = f'{serial} ({details})' if details else serial
                self.device_combo.addItem(label, serial)

        # 기존에 선택했던 장치가 아직 연결되어 있으면 선택 유지
        index = self.device_combo.findData(previous_device.split(' (')[0])
        if index >= 0:
            self.device_combo.setCurrentIndex(index)

    # ----------------------------------------------------------- recording
    def start_recording(self):
        if self.recording:
            return
        adb = self.resolve_adb()
        scrcpy = self.resolve_scrcpy()
        device = self.device_combo.currentText()
        if not adb or not device:
            QtWidgets.QMessageBox.warning(self, '오류', 'ADB 경로나 디바이스를 확인하세요. 에뮬레이터를 켠 뒤 새로고침하세요.')
            return
        if not scrcpy:
            QtWidgets.QMessageBox.warning(self, '오류', '내장 scrcpy를 찾을 수 없습니다. scrcpy 폴더 구성을 확인하세요.')
            return

        help_text = self.scrcpy_help(scrcpy)
        record_audio = self.keep_device_audio_check.isChecked()
        if help_text.strip() and record_audio and '--audio-source' not in help_text:
            QtWidgets.QMessageBox.warning(self, '옵션 미지원', '현재 scrcpy는 --audio-source 옵션을 지원하지 않습니다. scrcpy 2.x 이상을 사용하세요.')
            return
        if help_text.strip() and not record_audio and '--no-audio' not in help_text:
            QtWidgets.QMessageBox.warning(self, '옵션 미지원', '현재 scrcpy는 --no-audio 옵션을 지원하지 않습니다.')
            return

        out_dir = self.out_dir_edit.text().strip() or app_dir()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, '오류', f'저장 폴더를 만들 수 없습니다.\n{e}')
            return

        fmt = self.format_combo.currentText()
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
        self._base_out_path = os.path.join(out_dir, f'record_{stamp}.{fmt}')
        self._base_log_stem = os.path.join(out_dir, f'record_{stamp}')
        # scrcpy는 녹화 내내 하나의 파일에 기록합니다. pause 구간은 종료 후 FFmpeg로 잘라냅니다.
        self._current_segment_path = os.path.join(out_dir, f'record_{stamp}_raw.{fmt}')
        self._current_segment_log = self._base_log_stem + '.log'
        self._pause_intervals = []
        self._pause_started_at = None
        self._timeline_start = time.monotonic()
        self._last_display_second = -1
        self.paused = False
        self.stop_requested = False
        self.recording = True
        self.proc = None
        self._forced_kill = False
        self._active_elapsed = 0.0
        self._segment_started_at = self._timeline_start
        self.rec_started_at = self._timeline_start

        cmd = [scrcpy, '-s', device, '--no-window', '--no-control',
               f'--record={self._current_segment_path}', f'--record-format={fmt}']
        if record_audio:
            cmd += ['--audio-source=playback', '--audio-codec=aac']
            self._audio_mode = '기기 내부 소리 녹화 (playback/AAC)'
        else:
            cmd += ['--no-audio']
            self._audio_mode = '소리 녹화 끔 (영상만)'
        max_size = self.size_combo.currentData()
        if max_size:
            cmd += ['-m', str(max_size)]
        env = os.environ.copy()
        env['ADB'] = adb

        self.status_label.setText('🔴 녹화 중... 00:00:00')
        self.status_label.setStyleSheet('color: red; font-size: 16px;')
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText('일시 정지')
        self.stop_btn.setEnabled(True)
        self.keep_device_audio_check.setEnabled(False)
        # 100ms마다 단조 증가 시계를 확인하고 표시 초가 바뀔 때만 갱신해 타이머가 튀는 현상을 줄임.
        self.clock.start(100)
        threading.Thread(target=self.record_worker,
                         args=(cmd, env, self._current_segment_path, self._current_segment_log, self._audio_mode),
                         daemon=True).start()

    @staticmethod
    def _format_elapsed(seconds):
        sec = max(0, int(seconds))
        return f'{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}'

    def _elapsed_now(self):
        if not self._timeline_start:
            return 0.0
        return max(0.0, time.monotonic() - self._timeline_start)

    def toggle_pause_resume(self):
        if not self.recording or self.stop_requested:
            return
        if not self.paused:
            self.paused = True
            self._pause_started_at = self._elapsed_now()
            self.pause_btn.setText('녹화 재개')
            self.status_label.setStyleSheet('color: #b7791f; font-size: 16px;')
        else:
            now = self._elapsed_now()
            if self._pause_started_at is not None:
                self._pause_intervals.append((self._pause_started_at, now))
            self._pause_started_at = None
            self.paused = False
            self.pause_btn.setText('일시 정지')
            self.status_label.setStyleSheet('color: red; font-size: 16px;')
        self.update_clock()

    def record_worker(self, cmd, env, out_path, log_path, audio_mode):
        tail = deque(maxlen=35)
        try:
            kwargs = {}
            if IS_WIN:
                # scrcpy에는 Ctrl+C를 보낼 별도 콘솔이 필요하므로 새 콘솔을 만들되,
                # STARTF_USESHOWWINDOW로 창 표시만 숨깁니다. FFmpeg에는 이 설정을
                # 적용하지 않고 _run_ffmpeg의 CREATE_NO_WINDOW만 사용합니다.
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0
                kwargs['startupinfo'] = startup
                kwargs['creationflags'] = CREATE_NEW_CONSOLE
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, **kwargs)
            self.proc = proc
        except OSError as e:
            self.recording_finished.emit('finalized_error', f'scrcpy 실행 실패:\n{e}')
            return

        self.proc_started_at = time.monotonic()
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
        exists = os.path.isfile(out_path) and os.path.getsize(out_path) > 0
        detail = '\n'.join(tail) or '(scrcpy 출력 없음)'
        if exists and return_code == 0:
            self.recording_finished.emit('raw_ready', f'{out_path}\n{log_path}')
        else:
            self.recording_finished.emit('finalized_error',
                f'녹화가 정상 완료되지 않았습니다. 종료 코드: {return_code}\n로그: {log_path}\n\n최근 출력:\n{detail}')

    def update_clock(self):
        if not self.recording:
            return
        elapsed = self._elapsed_now()
        # 일시정지 구간을 제외한 유효 녹화 시간 표시. monotonic 시계를 사용해 시스템 시각 변경의 영향을 받지 않음.
        paused_total = sum(max(0.0, end - start) for start, end in self._pause_intervals)
        if self.paused and self._pause_started_at is not None:
            paused_total += max(0.0, elapsed - self._pause_started_at)
        active = max(0.0, elapsed - paused_total)
        sec = int(active)
        if sec != self._last_display_second:
            state = '⏸️ 일시 정지' if self.paused else '🔴 녹화 중...'
            self.status_label.setText(f'{state} {self._format_elapsed(active)}')
            self._last_display_second = sec

    def stop_recording(self):
        if not self.recording or self.stop_requested:
            return
        if self.paused and self._pause_started_at is not None:
            self._pause_intervals.append((self._pause_started_at, self._elapsed_now()))
            self._pause_started_at = None
        self.stop_requested = True
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.status_label.setText('💾 녹화 종료 및 저장 중...')
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self):
        deadline = time.monotonic() + 15
        while self.proc is None:
            if not self.recording or time.monotonic() >= deadline:
                return
            time.sleep(0.05)
        wait = MIN_RUN_BEFORE_STOP - (time.monotonic() - self.proc_started_at)
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

    def _begin_finalize(self):
        pass

    def _resolve_ffmpeg(self):
        """FFmpeg를 찾고, PyInstaller 임시 폴더에서 직접 실행하지 않도록 영구 폴더로 복사합니다."""
        exe_name = 'ffmpeg.exe' if IS_WIN else 'ffmpeg'
        roots = []
        if getattr(sys, 'frozen', False):
            roots.append(os.path.join(getattr(sys, '_MEIPASS', ''), 'ffmpeg', 'bin'))
        roots.append(os.path.join(app_dir(), 'ffmpeg', 'bin'))
        roots.append(os.path.join(app_dir(), 'ffmpeg'))
        source = next((os.path.join(root, exe_name) for root in roots
                       if os.path.isfile(os.path.join(root, exe_name))), None)
        if not source:
            return shutil.which('ffmpeg')
        if not getattr(sys, 'frozen', False):
            return source
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        target_dir = os.path.join(base, 'ADBRecorder', 'ffmpeg')
        target = os.path.join(target_dir, exe_name)
        try:
            os.makedirs(target_dir, exist_ok=True)
            if (not os.path.isfile(target) or
                    os.path.getsize(target) != os.path.getsize(source)):
                shutil.copyfile(source, target)
            # FFmpeg 실행 폴더의 DLL도 함께 복사합니다.
            for name in os.listdir(os.path.dirname(source)):
                src_file = os.path.join(os.path.dirname(source), name)
                if os.path.isfile(src_file) and name.lower().endswith('.dll'):
                    dst_file = os.path.join(target_dir, name)
                    if not os.path.isfile(dst_file) or os.path.getsize(dst_file) != os.path.getsize(src_file):
                        shutil.copyfile(src_file, dst_file)
            return target
        except OSError:
            # 임시 폴더 실행을 피할 수 없으면 원본 경로를 반환하되 실행 오류를 보여줍니다.
            return source

    @staticmethod
    def _run_ffmpeg(cmd):
        """FFmpeg를 실행하되 Windows 콘솔 창을 표시하지 않습니다."""
        kwargs = {
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'text': True,
            'errors': 'replace',
            'timeout': 24 * 60 * 60,
        }
        if IS_WIN:
            # CREATE_NO_WINDOW 자체로 콘솔 생성을 막습니다.
            # STARTUPINFO/STARTF_USESHOWWINDOW를 함께 지정하면 일부 Windows/PyInstaller
            # 환경에서 자식 프로세스의 표준 핸들이 유효하지 않게 되어 WinError 6이
            # 발생할 수 있으므로 startupinfo는 전달하지 않습니다.
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            # FFmpeg가 실행 파일 옆 DLL을 찾을 수 있도록 실행 폴더를 지정합니다.
            kwargs['cwd'] = os.path.dirname(os.path.abspath(cmd[0]))
        return subprocess.run(cmd, **kwargs)

    def _finalize_worker(self):
        raw_path = self._current_segment_path
        final_path = self._base_out_path
        if not raw_path or not os.path.isfile(raw_path) or os.path.getsize(raw_path) == 0:
            self.recording_finished.emit('finalized_error', '원본 녹화 파일이 없습니다. 원본 파일과 로그를 확인하세요.')
            return
        if not self._pause_intervals:
            try:
                # 일시정지가 없으면 FFmpeg 재인코딩 없이 파일 데이터만 복사합니다.
                temp = final_path + '.tmp' + os.path.splitext(final_path)[1]
                shutil.copyfile(raw_path, temp)
                os.replace(temp, final_path)
                self.recording_finished.emit('finalized_ok', f'녹화 저장 완료:\n{final_path}\n\n일시정지 구간: 없음')
            except Exception as e:
                self.recording_finished.emit('finalized_error', f'최종 파일 저장 실패: {e}\n원본 보존: {raw_path}')
            return

        ffmpeg = self._resolve_ffmpeg()
        if not ffmpeg:
            self.recording_finished.emit('finalized_error', f'FFmpeg를 찾을 수 없습니다. ffmpeg\\bin\\ffmpeg.exe를 내장하세요.\n원본 보존: {raw_path}')
            return
        try:
            total = self._elapsed_now()
            # 마지막 pause interval은 stop_recording에서 닫힘. 각 pause 구간을 제외한 부분만 남김.
            pauses = sorted((max(0.0, a), min(total, b)) for a, b in self._pause_intervals if b > a)
            active = []
            cursor = 0.0
            for a, b in pauses:
                a = max(cursor, a)
                if a > cursor + 0.08:
                    active.append((cursor, a))
                cursor = max(cursor, b)
            if total > cursor + 0.08:
                active.append((cursor, total))
            if not active:
                raise RuntimeError('저장할 녹화 구간이 없습니다. 일시정지 상태와 녹화 시간을 확인하세요.')

            # concat filter로 비디오/오디오를 같은 구간에서 자르고 재인코딩합니다.
            filters = []
            labels = []
            for i, (a, b) in enumerate(active):
                filters.append(f'[0:v:0]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]')
                if self.keep_device_audio_check.isChecked():
                    filters.append(f'[0:a:0]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]')
                labels.append(f'[v{i}]')
                if self.keep_device_audio_check.isChecked():
                    labels.append(f'[a{i}]')
            if self.keep_device_audio_check.isChecked():
                inputs = ''.join(f'[v{i}][a{i}]' for i in range(len(active)))
                filters.append(f'{inputs}concat=n={len(active)}:v=1:a=1[outv][outa]')
            else:
                filters.append(''.join(f'[v{i}]' for i in range(len(active))) + f'concat=n={len(active)}:v=1:a=0[outv]')
            temp_final = final_path + '.processing' + os.path.splitext(final_path)[1]
            cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', raw_path,
                   '-filter_complex', ';'.join(filters), '-map', '[outv]']
            if self.keep_device_audio_check.isChecked():
                cmd += ['-map', '[outa]', '-c:a', 'aac', '-b:a', '192k']
            cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18']
            if final_path.lower().endswith('.mp4'):
                cmd += ['-movflags', '+faststart']
            cmd += [temp_final]
            result = self._run_ffmpeg(cmd)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or 'FFmpeg가 구간을 자르지 못했습니다.')[-5000:])
            if not os.path.isfile(temp_final) or os.path.getsize(temp_final) == 0:
                raise RuntimeError('FFmpeg 결과 파일이 비어 있습니다.')
            os.replace(temp_final, final_path)
            try:
                os.remove(raw_path)
            except OSError:
                pass
            self.recording_finished.emit('finalized_ok',
                f'녹화 저장 완료:\n{final_path}\n\n일시정지 구간 {len(pauses)}개를 제거했습니다.\n원본 파일은 최종 저장 후 정리했습니다.')
        except Exception as e:
            self.recording_finished.emit('finalized_error',
                f'최종 파일 저장 중 오류가 발생했습니다.\n{e}\n\n원본 녹화 파일은 보존했습니다:\n{raw_path}')

    def on_recording_finished(self, kind, message):
        if kind == 'raw_ready':
            self.proc = None
            self.status_label.setText('💾 일시정지 구간 제거 및 저장 중...')
            threading.Thread(target=self._finalize_worker, daemon=True).start()
            return
        if kind == 'finalized_ok':
            self.clock.stop()
            self.recording_end()
            QtWidgets.QMessageBox.information(self, '완료', message)
            return
        if kind == 'finalized_error':
            self.clock.stop()
            self.recording_end()
            QtWidgets.QMessageBox.warning(self, '오류', message)
            return

    def recording_end(self):
        self.recording = False
        self.paused = False
        self.stop_requested = False
        self.proc = None
        self._segment_started_at = 0.0
        self._timeline_start = 0.0
        self._pause_started_at = None
        self.status_label.setText('⏹️ 대기 중')
        self.status_label.setStyleSheet('color: black; font-size: 16px;')
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText('일시 정지')
        self.stop_btn.setEnabled(False)
        self.keep_device_audio_check.setEnabled(True)

    def closeEvent(self, event):
        if self.recording:
            self.stop_recording()
            deadline = time.time() + GRACEFUL_STOP_TIMEOUT + 24 * 60 * 60
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
