import os
import random
from PySide6.QtCore import QObject, QTimer
from app.ui.go_pet_overlay import GoPetCaptureWindow, GoPetOverlay
from app.logic.sound_player import SoundPlayer

class GoPetManager(QObject):
    def __init__(self, config_manager):
        super().__init__()
        self.config_manager = config_manager
        self.overlay = GoPetOverlay()
        self.capture_window = GoPetCaptureWindow()
        self.sound_player = SoundPlayer()
        
        self.enabled = False
        self.display_mode = "game"
        self.events_config = {}
        self.current_priority = -1
        
        # State tracking
        self.is_flashed = False
        self.is_dead = False
        self.health = 100
        self.phase = ""
        
        self.priority_map = {
            "flashed": 80,
            "low_health": 60,
            "kill": 70,
            "rich": 40,
            "poor": 40,
            "normal": 0
        }
        
        # Timer to reset transient events (like kill)
        self.reset_timer = QTimer(self)
        self.reset_timer.timeout.connect(self._reset_to_persistent_state)
        self.reset_timer.setSingleShot(True)
        
        self._update_config()
        
    def _update_config(self):
        config = self.config_manager.get('go_pet', {})
        self.enabled = config.get('enabled', False)
        self.display_mode = config.get('display_mode', 'game')
        
        self.overlay.set_size(config.get('size', 200))
        self.overlay.set_position(config.get('offset_x', 50), config.get('offset_y', 50))
        self.capture_window.set_size(config.get('size', 200))
        
        events_list = config.get('events', [])
        if isinstance(events_list, list):
            self.events_config = {}
            for e in events_list:
                if isinstance(e, dict) and e.get('type') in self.priority_map:
                    self.events_config[e['type']] = e
        else:
            self.events_config = {}
        
        if self.enabled:
            if self.display_mode == "capture":
                self.overlay.hide_pet()
                self.capture_window.show_output_shell()
            else:
                self.capture_window.hide_output_shell()
            self._reset_to_persistent_state()
        else:
            self.overlay.hide_pet()
            self.capture_window.hide_output_shell()
            
    def set_enabled(self, enabled):
        self.enabled = enabled
        if enabled:
            self._update_config()
        else:
            self.overlay.hide_pet()
            self.capture_window.hide_output_shell()

    def _get_active_display(self):
        return self.capture_window if self.display_mode == "capture" else self.overlay

    def _hide_inactive_display(self):
        if self.display_mode == "capture":
            self.overlay.hide_pet()
            self.capture_window.show_output_shell()
        else:
            self.capture_window.hide_output_shell()

    def _is_low_health_active(self, health=None):
        if health is None:
            health = self.health
        if "low_health" not in self.events_config:
            return False
        threshold = self.events_config["low_health"].get("threshold", 30)
        return health < threshold

    def _parse_float(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _get_random_file(self, path, is_sound=False):
        if not path or not os.path.exists(path):
            return None
            
        if os.path.isdir(path):
            if is_sound:
                exts = {'.mp3', '.wav'}
            else:
                exts = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp', '.mp4', '.webm', '.avi', '.mov', '.mkv'}
                
            files = [os.path.join(path, f) for f in os.listdir(path) if os.path.splitext(f)[1].lower() in exts]
            if not files:
                return None
            return random.choice(files)
        return path
        
    def _trigger_event(self, event_type, transient=False):
        if not self.enabled or event_type not in self.priority_map:
            return
            
        priority = self.priority_map.get(event_type, 0)
        
        # Only override if priority is strictly higher, unless it's normal
        if priority >= self.current_priority or event_type == "normal":
            self.current_priority = priority
            active_display = self._get_active_display()
            self._hide_inactive_display()
            
            cfg = self.events_config.get(event_type)
            if cfg:
                img = self._get_random_file(cfg.get('image'), is_sound=False)
                snd = self._get_random_file(cfg.get('sound'), is_sound=True)
                
                if img:
                    active_display.update_pet(img)
                if snd:
                    self.sound_player.play_sound(snd, volume=100, channel="pet")
            elif event_type == "normal" and self.display_mode == "capture":
                self.capture_window.show_output_shell()
                    
            if transient:
                self.reset_timer.start(3000) # 3 seconds
            else:
                self.reset_timer.stop()

    def _reset_to_persistent_state(self):
        # Re-evaluate persistent state
        self.current_priority = -1 # Reset priority to allow re-evaluation
        
        if self.is_flashed and not self.is_dead:
            self._trigger_event("flashed")
        elif self._is_low_health_active() and not self.is_dead:
            self._trigger_event("low_health")
        else:
            self._trigger_event("normal")

    def process_gsi(self, game_state):
        if not self.enabled:
            return
            
        player = game_state.get('player', {})
        if not player:
            return
            
        state = player.get('state', {})
        round_info = game_state.get('round', {})
        phase = round_info.get('phase', '')
        match_stats = player.get('match_stats', {})
        
        # Observation check
        spectarget = player.get('spectarget')
        activity = player.get('activity', '')
        has_valid_team = player.get('team', '') in ['T', 'CT']
        provider = game_state.get('provider', {})
        is_observing = (spectarget is not None or activity == 'spectating' or activity != 'playing' or not has_valid_team)
        
        if is_observing:
            return

        # Phase tracking
        if phase != self.phase:
            self.phase = phase
            self._reset_to_persistent_state()
        
        # Health & Death
        health = state.get('health', 100)
        was_low_health = self._is_low_health_active()
        if health == 0 and not self.is_dead:
            self.is_dead = True
            self._reset_to_persistent_state()
        elif health > 0 and self.is_dead:
            self.is_dead = False
            self._reset_to_persistent_state()
            
        self.health = health
        is_low_health = self._is_low_health_active()
        if is_low_health != was_low_health and not self.is_dead:
            self._reset_to_persistent_state()
        
        # Flashed
        flashed = state.get('flashed', 0) > 0
        if flashed and not self.is_flashed:
            self.is_flashed = True
            self._trigger_event("flashed")
        elif not flashed and self.is_flashed:
            self.is_flashed = False
            self._reset_to_persistent_state()
            
        # Kills
        kills = match_stats.get('kills', 0)
        if not hasattr(self, '_last_kills'):
            self._last_kills = kills
        if kills > self._last_kills:
            self._last_kills = kills
            self._trigger_event("kill", transient=True)
            
        # Economy (only evaluate during freezetime)
        if phase == "freezetime":
            money = state.get('money', 0)
            if "rich" in self.events_config and money >= self.events_config["rich"].get("threshold", 10000):
                self._trigger_event("rich", transient=True)
            elif "poor" in self.events_config and money <= self.events_config["poor"].get("threshold", 2000):
                self._trigger_event("poor", transient=True)
                
        # main_window processes visual_handler before go_pet_manager.
        # Re-raise the pet overlay at the end of each GSI frame so flash/death
        # overlays do not cover the pet.
        if self.display_mode == "game":
            self.overlay.ensure_on_top()
