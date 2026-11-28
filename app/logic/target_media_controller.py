"""Control one selected app's media session, never the global media key."""
import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import ntpath
import queue
import re
import threading
import time
from urllib.parse import urlparse

from .game_window import GameWindowTracker


BROWSERS = {'msedge.exe', 'chrome.exe', 'firefox.exe', '360se.exe',
            'iexplore.exe', 'sogouexplorer.exe', 'browser.exe', 'yandex.exe',
            'opera.exe', 'brave.exe'}


@dataclass(frozen=True)
class MediaTarget:
    process_name: str
    window_title: str = ''
    hwnd: int = 0
    url: str = ''


def default_media_target(target_type, target):
    """Remember navigation intent before a slow page has a window/session."""
    try:
        if target_type == 'app':
            if target.lower().endswith('.lnk'):
                import win32com.client
                target = win32com.client.Dispatch('WScript.Shell').CreateShortCut(target).Targetpath
            return MediaTarget(ntpath.basename(target).lower())
        import winreg
        association = r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, association) as key:
            prog_id = winreg.QueryValueEx(key, 'ProgId')[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id + r'\shell\open\command') as key:
            command = winreg.QueryValueEx(key, '')[0]
        match = re.match(r'\s*(?:"([^"]+\.exe)"|([^\s]+\.exe))', command, re.IGNORECASE)
        process = ntpath.basename(match.group(1) or match.group(2)).lower() if match else ''
        if process in BROWSERS:
            return MediaTarget(process, url=target)
    except (OSError, AttributeError):
        pass
    return None


def refreshed_target(target):
    if not target.hwnd:
        return target
    tracker = GameWindowTracker(target.process_name)
    if not tracker.is_game_window(target.hwnd):
        return None
    tracker.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    tracker.user32.GetWindowTextW.restype = ctypes.c_int
    title = ctypes.create_unicode_buffer(1024)
    tracker.user32.GetWindowTextW(target.hwnd, title, len(title))
    return MediaTarget(target.process_name, title.value, target.hwnd, target.url)


def game_in_foreground():
    return bool(GameWindowTracker().foreground_game())


def website_words(url):
    host = urlparse(url if '://' in url else 'https://' + url).hostname or ''
    sites = {'douyin.com': ('抖音', 'douyin'), 'bilibili.com': ('哔哩', 'bilibili'),
             'youtube.com': ('youtube',)}
    return next((names for domain, names in sites.items()
                 if host == domain or host.endswith('.' + domain)), (host,) if host else ())


def find_media_windows(target, preferred_hwnd=None):
    """Find site-identified windows, including newly opened video windows."""
    tracker = GameWindowTracker(target.process_name)
    user32 = tracker.user32
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    words = website_words(target.url) if target.url else ()
    def candidate(hwnd):
        if not user32.IsWindowVisible(hwnd) or not tracker.is_game_window(hwnd):
            return None
        title = ctypes.create_unicode_buffer(1024)
        user32.GetWindowTextW(hwnd, title, len(title))
        if words and not any(word in title.value.casefold() for word in words):
            return None
        return MediaTarget(target.process_name, title.value, int(hwnd), target.url)
    matches = []
    if preferred_hwnd:
        found = candidate(preferred_hwnd)
        if found:
            matches.append(found)
    @tracker.enum_proc
    def visit(hwnd, _):
        if hwnd == preferred_hwnd:
            return True
        found = candidate(hwnd)
        if found:
            matches.append(found)
        return True
    user32.EnumWindows(visit, 0)
    return matches


def find_media_window(target, preferred_hwnd=None):
    matches = find_media_windows(target, preferred_hwnd)
    return matches[0] if matches else None


def activate_media_window(target, preferred_hwnd=None):
    found = find_media_window(target, preferred_hwnd)
    if found is None:
        return None
    tracker = GameWindowTracker(target.process_name)
    user32, kernel32 = tracker.user32, tracker.kernel32
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    if user32.IsIconic(found.hwnd):
        class Placement(ctypes.Structure):
            _fields_ = [('length', wintypes.UINT), ('flags', wintypes.UINT),
                        ('showCmd', wintypes.UINT), ('ptMinPosition', wintypes.POINT),
                        ('ptMaxPosition', wintypes.POINT), ('rcNormalPosition', wintypes.RECT)]
        placement = Placement()
        placement.length = ctypes.sizeof(placement)
        user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(Placement)]
        user32.GetWindowPlacement.restype = wintypes.BOOL
        user32.GetWindowPlacement(found.hwnd, ctypes.byref(placement))
        user32.ShowWindow(found.hwnd, 3 if placement.flags & 2 else 9)
    user32.SetForegroundWindow(found.hwnd)
    if user32.GetForegroundWindow() != found.hwnd:
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        current = kernel32.GetCurrentThreadId()
        foreground = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        attached = bool(foreground and foreground != current and user32.AttachThreadInput(current, foreground, True))
        try:
            user32.SetForegroundWindow(found.hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current, foreground, False)
    # Foreground activation can complete after the current UI callback returns.
    # A found window is not a missing page just because focus has not settled.
    return found


def foreground_media_target(target_type, target):
    """Bind the request to the app actually brought to the foreground."""
    tracker = GameWindowTracker()
    hwnd = tracker.user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    tracker.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = tracker.kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return None
    try:
        path = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(path))
        if not tracker.kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return None
        process = ntpath.basename(path.value).lower()
    finally:
        tracker.kernel32.CloseHandle(handle)
    if target_type == 'url':
        if process not in BROWSERS:
            return None
    else:
        configured = target
        if configured.lower().endswith('.lnk'):
            import win32com.client
            configured = win32com.client.Dispatch('WScript.Shell').CreateShortCut(configured).Targetpath
        if process != ntpath.basename(configured).lower():
            return None
    tracker.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    tracker.user32.GetWindowTextW.restype = ctypes.c_int
    title = ctypes.create_unicode_buffer(1024)
    tracker.user32.GetWindowTextW(hwnd, title, len(title))
    return MediaTarget(process, title.value, int(hwnd))


def source_matches(process_name, source_id):
    stem = ntpath.splitext(ntpath.basename(process_name.lower()))[0]
    aliases = {'msedge': ('msedge', 'microsoftedge')}.get(stem, (stem,))
    return any(re.search(r'(^|[^a-z0-9])' + re.escape(alias) + r'($|[^a-z0-9])',
                         source_id.lower()) for alias in aliases)


async def matching_sessions(sessions, target, titles=None):
    candidates = [session for session in sessions
                  if source_matches(target.process_name, session.source_app_user_model_id)]
    site = website_words(target.url) if target.url else ()
    if len(candidates) == 1 and (target.process_name not in BROWSERS
                               or (not target.window_title and not target.url)):
        return candidates
    # Several sessions from the same browser: require a unique active-tab title
    # match instead of choosing the system's current (possibly music) session.
    matches = []
    titles = {} if titles is None else titles
    for session in candidates:
        if id(session) not in titles:
            properties = await asyncio.wait_for(session.try_get_media_properties_async(), timeout=2)
            titles[id(session)] = properties.title.strip().casefold()
        title = titles[id(session)]
        if title and ((site and (any(name in title for name in site)
                                or (title in target.window_title.casefold()
                                    and any(name in target.window_title.casefold() for name in site))))
                      or (not site and title in target.window_title.casefold())):
            matches.append(session)
    return matches


async def select_session(sessions, target):
    matches = await matching_sessions(sessions, target)
    return matches[0] if len(matches) == 1 else None


class TargetMediaController:
    def __init__(self, manager_factory=None, report=None, foreground_check=game_in_foreground, browser_bridge=None):
        self._manager_factory = manager_factory
        self._report = report or (lambda *args, **kwargs: None)
        self._commands = queue.Queue()
        self._lock = threading.RLock()
        self._generation = 0
        self._thread = None
        self._session = None
        self._target = None
        self._closed = False
        self._target_hint = None
        self._desired_playing = True
        self._foreground_check = foreground_check
        self._manager = None
        self._last_guard_error = ''
        self._related_windows = []
        self._related_key = None
        self._related_checked = 0.0
        self._last_wait_report = None
        self._bridge = browser_bridge
        if browser_bridge is not None:
            browser_bridge.failed.connect(self._extension_fallback)

    def _extension_fallback(self, reason):
        with self._lock:
            if self._closed or self._target_hint is None:
                return
            self._ensure_worker()
            command = 'play' if self._desired_playing else 'pause'
            self._commands.put((command, self._generation, self._target_hint))

    def _ensure_worker(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='TargetMediaControl', daemon=True)
            self._thread.start()

    def prepare(self, target):
        with self._lock:
            if self._closed:
                return
            self._generation += 1
            self._desired_playing = True
            self._target_hint = target
            if target is not None:
                self._ensure_worker()

    def _current(self, generation):
        with self._lock:
            return not self._closed and generation == self._generation

    def play(self, target):
        with self._lock:
            if self._closed:
                return
            self._generation += 1
            generation = self._generation
            if (not target.url and self._target_hint is not None
                    and target.process_name == self._target_hint.process_name):
                target = MediaTarget(target.process_name, target.window_title,
                                     target.hwnd, self._target_hint.url)
            self._target_hint = target
            self._desired_playing = True
            self._ensure_worker()
            self._commands.put(('play', generation, target))

    def pause(self):
        with self._lock:
            self._generation += 1
            self._desired_playing = False
            worker_running = self._thread is not None and not self._closed
            target_known = self._target_hint is not None
            extension_handled = bool(self._bridge is not None and self._bridge.pause())
            if worker_running and not extension_handled:
                self._commands.put(('pause', self._generation, None))
        self._report('boss_media_pause_requested', worker_running=worker_running,
                     target_known=target_known)

    @staticmethod
    async def _get_manager():
        # These imports register the WinRT async/collection projections too.
        import winrt.windows.foundation
        import winrt.windows.foundation.collections
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
        return await GlobalSystemMediaTransportControlsSessionManager.request_async()

    async def _play(self, generation, target):
        if not self._current(generation):
            return
        if self._session is not None and self._target != target:
            await self._pause()
        manager = await self._session_manager()
        session = self._session
        for _ in range(12):
            if not self._current(generation):
                return
            session = await select_session(manager.get_sessions(), target)
            if session is not None:
                break
            await asyncio.sleep(0.25)
        if session is None:
            self._report('boss_media_unavailable', process=target.process_name)
            return
        if not self._current(generation):
            return
        self._session = session
        self._target = target
        # PLAYING=4. Silence/volume is not a playback-state detector.
        ok = (int(session.get_playback_info().playback_status) == 4
              or await asyncio.wait_for(session.try_play_async(), timeout=3))
        self._report('boss_media_play', process=target.process_name, success=bool(ok))
        if not ok:
            self._session = None
            self._target = None
        elif not self._current(generation):
            # A respawn during an asynchronous play request must undo it.
            await self._pause()

    async def _pause(self):
        session, self._session = self._session, None
        target = self._target
        self._target = None
        if session is None:
            self._report('boss_media_pause', success=False, reason='no_bound_session')
            return
        status = int(session.get_playback_info().playback_status)
        ok = (status in (0, 3, 5)
              or await asyncio.wait_for(session.try_pause_async(), timeout=3))
        self._report('boss_media_pause', success=bool(ok), status_before=status,
                     process=target.process_name if target is not None else '')

    async def _session_manager(self):
        if self._manager is None:
            self._manager = await asyncio.wait_for((self._manager_factory or self._get_manager)(), timeout=3)
        return self._manager

    async def _guard_late_media(self):
        if self._bridge is not None and self._bridge.controls_media:
            return
        with self._lock:
            if self._closed or self._desired_playing or self._target_hint is None:
                return
            target, generation = self._target_hint, self._generation
        if not self._foreground_check():
            return
        refreshed = refreshed_target(target)
        if refreshed is None and target.url:
            refreshed = MediaTarget(target.process_name, url=target.url)
        if refreshed is None:
            return
        target = refreshed
        manager = await self._session_manager()
        sessions = list(manager.get_sessions())
        titles = {}
        matches = await matching_sessions(sessions, target, titles)
        if target.url:
            key = target.process_name, target.url
            now = time.monotonic()
            if key != self._related_key or now - self._related_checked >= 1.0:
                self._related_windows = find_media_windows(target)
                self._related_key, self._related_checked = key, now
            # A homepage and its video may be in different browser windows.
            # Match only media identified by the configured website/windows.
            for related in self._related_windows:
                for session in await matching_sessions(sessions, related, titles):
                    if not any(session is existing for existing in matches):
                        matches.append(session)
        if not matches:
            status = (target.process_name, len(sessions), sum(
                source_matches(target.process_name, item.source_app_user_model_id) for item in sessions))
            if status != self._last_wait_report:
                self._last_wait_report = status
                self._report('boss_media_late_wait', reason='no_matching_session',
                             process=status[0], total_sessions=status[1], browser_sessions=status[2])
            return
        self._last_wait_report = None
        with self._lock:
            if not self._current(generation) or self._desired_playing:
                return
        for session in matches:
            if self._bridge is not None and self._bridge.controls_media:
                return
            if not self._current(generation) or self._desired_playing or not self._foreground_check():
                return
            if int(session.get_playback_info().playback_status) in (2, 4):
                ok = await asyncio.wait_for(session.try_pause_async(), timeout=3)
                self._report('boss_media_late_pause', process=target.process_name,
                             success=bool(ok), matched_sessions=len(matches))

    def _run(self):
        loop = None
        initialized = False
        try:
            from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
            init_apartment(ApartmentType.MULTI_THREADED)
            initialized = True
            loop = asyncio.new_event_loop()
            while True:
                try:
                    command, generation, target = self._commands.get(timeout=0.1)
                except queue.Empty:
                    try:
                        loop.run_until_complete(self._guard_late_media())
                        self._last_guard_error = ''
                    except Exception as error:
                        kind = type(error).__name__
                        if kind != self._last_guard_error:
                            self._report('boss_media_error', kind=kind)
                            self._last_guard_error = kind
                        self._manager = None
                    continue
                try:
                    if command in ('pause', 'close'):
                        loop.run_until_complete(self._pause())
                    else:
                        loop.run_until_complete(self._play(generation, target))
                except Exception as error:
                    self._report('boss_media_error', kind=type(error).__name__)
                finally:
                    self._commands.task_done()
                if command == 'close':
                    break
        except Exception as error:
            self._report('boss_media_error', kind=type(error).__name__)
        finally:
            self._session = None
            self._manager = None
            if loop is not None:
                loop.close()
            if initialized:
                uninit_apartment()
            with self._lock:
                self._thread = None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            worker = self._thread
            if worker is not None:
                self._commands.put(('close', self._generation, None))
        if self._bridge is not None:
            self._bridge.release()
            try:
                self._bridge.failed.disconnect(self._extension_fallback)
            except (RuntimeError, TypeError):
                pass
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1)
