"""GSI-driven local music cues; independent Python implementation.

Event slots and legacy import names follow the CS-Jukebox-V2 music-kit model:
https://github.com/TYFALY/CS-Jukebox-V2
"""
from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class EventSlot:
    key: str
    title: str
    hint: str
    duration: float
    loop: bool = False
    legacy: str = ''


SLOTS = (
    EventSlot('main_menu', '主菜单', '大厅或主菜单，适合循环音乐', 0, True, 'mainMenuSong'),
    EventSlot('freeze_time', '准备阶段', '每回合购买与准备阶段，参考 15 秒', 15, True, 'freezeSong'),
    EventSlot('round_start', '回合开始', '准备结束后播放，参考 5 秒', 5, False, 'startSong'),
    EventSlot('bomb_planted', 'C4 已安放', '安包后播放，参考 30 秒', 30, False, 'bombSong'),
    EventSlot('round_ten_seconds', '回合剩余十秒', '游戏倒计时优先，缺失时可按设置估算', 10, False, 'roundTenSecSong'),
    EventSlot('bomb_ten_seconds', 'C4 十秒警告 · 通用', '安包者未知或专用切片未设置时使用', 10, False, 'bombTenSecSong'),
    EventSlot('bomb_ten_seconds_self', 'C4 十秒警告 · 自己安放', '确认由自己安放时使用，否则使用通用切片', 10),
    EventSlot('bomb_ten_seconds_other', 'C4 十秒警告 · 其他人安放', '确认由其他玩家安放时使用，否则使用通用切片', 10),
    EventSlot('round_won', '回合胜利', '己方获胜，参考 7 秒', 7, False, 'winSong'),
    EventSlot('round_lost', '回合失败', '己方失败，参考 7 秒', 7, False, 'loseSong'),
    EventSlot('mvp', '获得 MVP', '自己的 MVP 次数增加，参考 7 秒', 7, False, 'MVPSong'),
    EventSlot('death', '玩家死亡', '自己阵亡时播放，参考 5 秒；安包音乐优先', 5, False, 'deathSong'),
)
SLOT_MAP = {slot.key: slot for slot in SLOTS}
PRIORITY = {'stop': 0, 'main_menu': 10, 'freeze_time': 20, 'round_start': 30, 'death': 40,
            'bomb_planted': 50, 'round_ten_seconds': 60, 'bomb_ten_seconds': 70,
            'bomb_ten_seconds_self': 70, 'bomb_ten_seconds_other': 70,
            'round_won': 80, 'round_lost': 80, 'mvp': 90}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and not isinstance(value, bool) else None
    except (ValueError, TypeError):
        return None


def object_node(value):
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class MusicCue:
    event: str
    started: float


class MusicEventEngine:
    def __init__(self, clock=time.monotonic, estimate_timers=True, bomb_seconds=40, round_seconds=115, warning_lead_ms=0):
        self.clock = clock
        self.estimate_timers = estimate_timers
        self.bomb_seconds = bomb_seconds
        self.round_seconds = round_seconds
        self.warning_lead_ms = warning_lead_ms
        self.reset()

    def reset(self):
        self.map_name = None
        self.round_number = None
        self.phase = ''
        self.activity = ''
        self.team = ''
        self.health = None
        self.mvps = None
        self.bomb_state = ''
        self.local_id = ''
        self.planting_player = ''
        self.planter_id = ''
        self.planter_source = ''
        self._local_inventory = None
        self._local_c4_loss = None
        self.local_plant_time = None
        self.server_bomb_planted = False
        self.plant_evidence = {}
        self.round_result = ''
        self.round_paused = False
        self.paused_at = None
        self.round_estimated = self.bomb_estimated = False
        self.round_deadline = self.bomb_deadline = None
        self.ten_played = set()
        self.current_cue = None
        self.last_update = None

    def _emit(self, event, now):
        self.current_cue = MusicCue(event, now)
        return self.current_cue

    def process(self, state):
        if not isinstance(state, dict) or not any(k in state for k in ('player', 'map', 'round', 'bomb')):
            return None
        now = self.clock()
        map_info = object_node(state.get('map'))
        map_name = map_info.get('name')
        if map_name and self.map_name and map_name != self.map_name:
            self.reset()
        if map_name:
            self.map_name = map_name
        round_number = map_info.get('round')
        if round_number is not None and round_number != self.round_number:
            self.ten_played.clear()
            self.round_deadline = self.bomb_deadline = None
            self.round_number = round_number
            self.planting_player = self.planter_id = ''
            self.planter_source = ''
            self._local_inventory = self._local_c4_loss = None
            self.local_plant_time = None
            if object_node(state.get('round')).get('phase') != 'over':
                self.round_result = ''
        self.last_update = now
        player = object_node(state.get('player'))
        round_info = object_node(state.get('round'))
        player_state = object_node(player.get('state'))
        bomb = object_node(state.get('bomb'))
        phase = round_info.get('phase', '')
        activity = player.get('activity', '')
        provider_id = str(object_node(state.get('provider')).get('steamid') or '')
        if provider_id:
            self.local_id = provider_id
        player_id = str(player.get('steamid') or '')
        local = (not provider_id or not player_id or provider_id == player_id)
        local = local and player.get('spectarget') in (None, 0, '0', -1, '-1', '', provider_id) and activity != 'spectating'
        if local and player.get('team') in ('T', 'CT'):
            self.team = player['team']
        health = number(player_state.get('health')) if local else None
        mvps = number(object_node(player.get('match_stats')).get('mvps')) if local else None
        raw_bomb_state = bomb.get('state', '')
        round_bomb_state = round_info.get('bomb', '')
        bomb_state = raw_bomb_state or round_bomb_state
        # round.bomb can announce a completed plant before the detailed bomb
        # node catches up. Do not wait for that second update to start music.
        if round_bomb_state in ('planted', 'defused', 'exploded') and raw_bomb_state in ('', 'carried', 'dropped', 'planting'):
            bomb_state = round_bomb_state
        planted = bomb_state in ('planted', 'defusing')
        self.server_bomb_planted = planted
        # The identity on a planting packet is the planter. The identity on a
        # defusing packet is the defuser, and must never replace the planter.
        if raw_bomb_state == 'planting' and self.bomb_state not in ('planted', 'defusing'):
            identity = str(bomb.get('player') or '')
            self.planting_player = identity if identity.isdecimal() and len(identity) == 17 else ''
        if planted and self.planting_player and self.bomb_state not in ('planted', 'defusing'):
            self.planter_id = self.planting_player
            self.planter_source = 'gsi'
            self.planting_player = ''
        elif bomb_state in ('carried', 'dropped', 'defused', 'exploded') and not (
                self.planter_source == 'inventory_reward' and self.bomb_state == 'planted'
                and bomb_state in ('carried', 'dropped')):
            self.planting_player = ''
            self.planter_id = ''
            self.planter_source = ''
        if bomb_state in ('defused', 'exploded'):
            self.local_plant_time = None
            self._local_c4_loss = None
        can_infer = phase not in ('over', 'freezetime') and bomb_state not in ('defused', 'exploded')
        self._track_local_plant(player, local, health, planted, now, can_infer)
        # The local inventory/reward update can precede round.bomb. Once a
        # completed local plant is proven, retain it through delayed bomb data.
        if can_infer and self.planter_source == 'inventory_reward' and self.local_plant_time is not None:
            bomb_state, planted = 'planted', True
        candidates = []
        entered_menu = activity == 'menu' and not phase and map_info.get('phase') not in ('live', 'warmup')
        if entered_menu:
            if self.activity != 'menu' or not self.current_cue or self.current_cue.event != 'main_menu':
                candidates.append('main_menu')
            self.phase = ''
            self.health = self.mvps = None
            self.round_deadline = self.bomb_deadline = None
            self.bomb_state = ''
            self.planting_player = self.planter_id = ''
            self.planter_source = ''
            self._local_inventory = self._local_c4_loss = None
            self.local_plant_time = None
            self.ten_played.clear()
        else:
            if self.activity == 'menu':
                candidates.append('stop')
            if phase == 'freezetime' and self.phase != 'freezetime':
                candidates.append('freeze_time')
                self.ten_played.clear()
                self.round_deadline = self.bomb_deadline = None
                self.planting_player = self.planter_id = ''
                self.planter_source = ''
                self._local_c4_loss = None
                self.local_plant_time = None
                self.round_result = ''
            elif phase == 'live' and self.phase == 'freezetime':
                candidates.append('round_start')
                if self.estimate_timers:
                    self.round_deadline = now + self.round_seconds
                    self.round_estimated = True
            if planted and self.bomb_state not in ('planted', 'defusing'):
                candidates.append('bomb_planted')
                self.ten_played.discard('bomb_ten_seconds')
                # Estimate only a newly observed plant, never a mid-round join.
                if self.estimate_timers and (self.phase == 'live' or self.local_plant_time is not None):
                    self.bomb_deadline = (self.local_plant_time if self.local_plant_time is not None else now) + self.bomb_seconds
                    self.bomb_estimated = True
            if (health == 0 and self.health is not None and self.health > 0
                    and phase == 'live' and not planted):
                candidates.append('death')
            if phase == 'over' and not self.round_result:
                winner = round_info.get('win_team')
                if winner in ('T', 'CT') and self.team:
                    self.round_result = 'round_won' if winner == self.team else 'round_lost'
                    if not (self.phase == 'over' and self.current_cue and self.current_cue.event == 'mvp'):
                        candidates.append(self.round_result)
            if (mvps is not None and self.mvps is not None and mvps > self.mvps
                    and phase == 'over'):
                candidates.append('mvp')
            if health is not None:
                self.health = health
            if mvps is not None:
                self.mvps = mvps
            if phase:
                self.phase = phase
            self.bomb_state = bomb_state
            countdown = object_node(state.get('phase_countdowns'))
            self.round_paused = countdown.get('phase') in ('paused', 'timeout_t', 'timeout_ct')
            if self.round_paused and self.paused_at is None:
                self.paused_at = now
            elif not self.round_paused and self.paused_at is not None:
                if self.round_estimated and self.round_deadline is not None:
                    self.round_deadline += now - self.paused_at
                self.paused_at = None
            remaining = number(countdown.get('phase_ends_in'))
            if remaining is not None and remaining >= 0:
                if countdown.get('phase') == 'live' and not planted:
                    self.round_deadline = now + remaining
                    self.round_estimated = False
                elif countdown.get('phase') == 'bomb' and planted:
                    self.bomb_deadline = now + remaining
                    self.bomb_estimated = False
            bomb_remaining = number(bomb.get('countdown'))
            if raw_bomb_state == 'planted' and bomb_remaining is not None and bomb_remaining >= 0:
                self.bomb_deadline = now + bomb_remaining
                self.bomb_estimated = False
            if phase in ('freezetime', 'over') or bomb_state in ('defused', 'exploded'):
                self.round_deadline = self.bomb_deadline = None
            if self.phase == 'live' and not self.round_paused:
                warning = self._warning(now)
                if warning:
                    candidates.append(warning)
        self.activity = activity
        if candidates:
            return self._emit(max(candidates, key=PRIORITY.__getitem__), now)
        if bomb_state in ('defused', 'exploded') and self.current_cue and self.current_cue.event.startswith('bomb_'):
            return self._emit('stop', now)
        return None

    def _track_local_plant(self, player, local, health, planted, now, can_infer=True):
        self.plant_evidence = {'local': bool(local), 'alive': health is not None and health > 0}
        if not local or health is None or health <= 0:
            self._local_inventory = self._local_c4_loss = None
            return
        weapons = player.get('weapons')
        if not isinstance(weapons, dict):
            self.plant_evidence['inventory_available'] = False
            return
        c4 = next((w for w in weapons.values() if isinstance(w, dict) and w.get('name') == 'weapon_c4'), None)
        stats = object_node(player.get('match_stats'))
        snapshot = {'has_c4': c4 is not None, 'active': c4 is not None and c4.get('state') == 'active',
                    'money': number(object_node(player.get('state')).get('money')),
                    'score': number(stats.get('score')), 'kills': number(stats.get('kills')),
                    'assists': number(stats.get('assists')), 'time': now}
        previous = self._local_inventory
        if c4 is not None:
            self._local_c4_loss = None
        elif previous and previous['has_c4'] and previous['active'] and now - previous['time'] <= 2.5:
            self._local_c4_loss = dict(previous, lost_at=now)
        evidence = self._local_c4_loss
        self.plant_evidence.update(inventory_available=True, c4_now=snapshot['has_c4'],
            c4_active_before=bool(previous and previous['active']), c4_loss=evidence is not None)
        if can_infer and not self.planter_id and self.local_id and evidence and now - evidence['time'] <= 2.5:
            no_combat_reward = all(snapshot[key] is not None and snapshot[key] == evidence[key]
                                   for key in ('kills', 'assists'))
            plant_reward = any(snapshot[key] is not None and evidence[key] is not None
                               and snapshot[key] - evidence[key] == reward
                               for key, reward in (('money', 300), ('score', 2)))
            self.plant_evidence.update(combat_unchanged=no_combat_reward, reward=plant_reward)
            if not snapshot['has_c4'] and no_combat_reward and plant_reward:
                self.planter_id = self.local_id
                self.planter_source = 'inventory_reward'
                self.local_plant_time = evidence['lost_at']
        self._local_inventory = snapshot

    def _warning(self, now):
        planted = self.bomb_state in ('planted', 'defusing')
        event = 'bomb_ten_seconds' if planted else 'round_ten_seconds'
        deadline = self.bomb_deadline if planted else self.round_deadline
        threshold = 10 + (self.warning_lead_ms / 1000 if planted else 0)
        if deadline is not None and 0 < deadline - now <= threshold and event not in self.ten_played:
            self.ten_played.add(event)
            if planted and self.planter_id and self.local_id:
                return event + ('_self' if self.planter_id == self.local_id else '_other')
            return event
        return None

    def tick(self):
        now = self.clock()
        if self.phase != 'live' or self.round_paused or self.last_update is None or now - self.last_update > 3:
            return None
        event = self._warning(now)
        return self._emit(event, now) if event else None
