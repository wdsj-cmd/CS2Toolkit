"""Cancel death switching on team/round end without requiring roster data."""
import time


class DeathSwitchGuard:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self):
        self.active = False
        self.local_id = ''
        self.team = ''
        self.context = None
        self.started = 0.0
        self.last_teammate = None
        self.last_update = None
        self.blocked = False
        self.reason = ''

    @staticmethod
    def _context(state):
        map_info = state.get('map', {})
        return map_info.get('name'), map_info.get('round')

    @staticmethod
    def _health(player):
        value = player.get('state', {}).get('health')
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def start(self, state):
        self.reset()
        self.active = True
        self.started = self.clock()
        player = state.get('player', {})
        self.local_id = str(state.get('provider', {}).get('steamid') or player.get('steamid') or '')
        self.team = player.get('team', '')
        self.context = self._context(state)
        self.update(state)

    def _block(self, reason):
        self.blocked = True
        self.reason = reason

    def update(self, state):
        if not self.active or self.blocked:
            return
        self.last_update = self.clock()
        phase = state.get('round', {}).get('phase', '')
        if phase in ('over', 'freezetime') or state.get('map', {}).get('phase') == 'gameover':
            self._block('round_ended')
            return
        context = self._context(state)
        if any(old is not None and new is not None and old != new
               for old, new in zip(self.context, context)):
            self._block('round_changed')
            return
        if self.team not in ('T', 'CT') or not self.local_id:
            return
        player = state.get('player', {})
        player_id = str(player.get('steamid') or '')
        health = self._health(player)
        if player_id == self.local_id and health is not None and health > 0:
            self._block('respawned')
            return
        roster = state.get('allplayers')
        if isinstance(roster, dict):
            teammates = [p for key, p in roster.items() if isinstance(p, dict)
                         and str(p.get('steamid') or key) != self.local_id
                         and p.get('team') == self.team]
            if any(self._health(p) is not None and self._health(p) > 0 for p in teammates):
                self.last_teammate = self.clock()
                return
            if teammates and all(self._health(p) == 0 for p in teammates):
                self._block('no_surviving_teammate')
                return
        # Ordinary players may not receive allplayers. A living observed
        # teammate is positive proof; an enemy/free camera is not.
        if (player_id and player_id != self.local_id and player.get('team') == self.team
                and health is not None and health > 0):
            self.last_teammate = self.clock()

    def decision(self):
        if not self.active or self.blocked:
            return 'skip'
        now = self.clock()
        if self.last_update is None or now - self.last_update > 2.0:
            self._block('stale_game_state')
            return 'skip'
        if self.last_teammate is not None and now - self.last_teammate <= 2.0:
            return 'allow'
        # A normal playing client often receives only its own player state.
        # Missing teammate telemetry must not suppress every normal death.
        if now - self.started >= 0.8:
            return 'allow'
        return 'wait'
