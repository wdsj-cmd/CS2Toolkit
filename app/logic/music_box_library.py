"""Managed music assets and portable kits, including CS-Jukebox-V2 ZIP import."""
import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import uuid
import zipfile

from .music_box_events import SLOTS, SLOT_MAP

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.wma', '.opus'}
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def validate_event_options(options, clip_count):
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError('事件播放设置格式不正确')
    random_play = options.get('random', True)
    selected = options.get('selected_index', 0)
    if not isinstance(random_play, bool) or type(selected) is not int:
        raise ValueError('随机播放或固定切片设置无效')
    return {'random': random_play,
            'selected_index': max(0, min(selected, clip_count - 1))}


def event_options(kit, event):
    return validate_event_options(kit.get('event_options', {}).get(event),
                                  len(kit['tracks'].get(event, [])))


def validate_clip(clip, event):
    if not isinstance(clip, dict):
        raise ValueError('切片配置格式不正确')
    slot = SLOT_MAP[event]
    result = {'path': str(clip.get('path', '')),
              'name': str(clip.get('name') or Path(str(clip.get('path', ''))).name)[:160]}
    for key, default, maximum in (('start', 0, 86400), ('duration', slot.duration, 3600), ('volume', 100, 100)):
        try:
            value = float(clip.get(key, default))
        except (TypeError, ValueError) as exc:
            raise ValueError('起始时间、时长或音量必须是数字') from exc
        if not math.isfinite(value) or not 0 <= value <= maximum:
            raise ValueError('起始时间、时长或音量超出范围')
        result[key] = value
    result['loop'] = bool(clip.get('loop', slot.loop))
    return result


def safe_relative(name):
    value = str(name).replace('\\', '/')
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or any(':' in p or '\0' in p for p in path.parts):
        raise ValueError('音乐包包含不安全的文件路径')
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{prefix}{i}' for prefix in ('COM', 'LPT') for i in range(1, 10)}
    if any(p.rstrip(' .') != p or p.split('.')[0].upper() in reserved for p in path.parts):
        raise ValueError('音乐包包含不支持的 Windows 文件名')
    return path


class MusicKitLibrary:
    def __init__(self, work_dir):
        self.root = Path(work_dir) / 'music_kits'
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, kit_id):
        if len(kit_id) != 32 or any(c not in '0123456789abcdef' for c in kit_id):
            raise ValueError('音乐盒标识无效')
        result = self.root / kit_id
        if not result.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('音乐盒路径无效')
        return result

    def _save(self, kit):
        folder = self.directory(kit['id'])
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / 'kit.json.tmp'
        temporary.write_text(json.dumps(kit, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(folder / 'kit.json')

    def list_kits(self):
        result = []
        for folder in self.root.iterdir():
            if folder.is_dir():
                try:
                    result.append(self.load(folder.name))
                except (ValueError, OSError, json.JSONDecodeError):
                    pass
        return sorted(result, key=lambda kit: kit['name'].casefold())

    def load(self, kit_id):
        data = json.loads((self.directory(kit_id) / 'kit.json').read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or not isinstance(data.get('tracks'), dict):
            raise ValueError('音乐盒配置无效')
        result = {'id': kit_id, 'name': str(data.get('name') or '未命名音乐盒')[:100],
                  'format': 'cs2toolkit.music_kit', 'version': 1, 'tracks': {}, 'event_options': {}}
        options = data.get('event_options', {})
        if not isinstance(options, dict):
            raise ValueError('事件播放设置格式不正确')
        for event in SLOT_MAP:
            clips = data['tracks'].get(event, [])
            if not isinstance(clips, list) or len(clips) > 64:
                raise ValueError('切片数量或格式无效')
            result['tracks'][event] = [validate_clip(clip, event) for clip in clips]
            result['event_options'][event] = validate_event_options(options.get(event), len(clips))
            for clip in result['tracks'][event]:
                self.resolve(result, clip)
        return result

    def create(self, name):
        if not name.strip():
            raise ValueError('请输入音乐盒名称')
        kit = {'id': uuid.uuid4().hex, 'name': name.strip()[:100], 'format': 'cs2toolkit.music_kit',
               'version': 1, 'tracks': {event: [] for event in SLOT_MAP},
               'event_options': {event: validate_event_options(None, 0) for event in SLOT_MAP}}
        self._save(kit)
        return kit

    def rename(self, kit_id, name):
        if not name.strip():
            raise ValueError('请输入音乐盒名称')
        kit = self.load(kit_id)
        kit['name'] = name.strip()[:100]
        self._save(kit)
        return kit

    def resolve(self, kit, clip):
        relative = safe_relative(clip['path'])
        base = self.directory(kit['id']).resolve()
        path = (base / str(relative)).resolve()
        if not path.is_relative_to(base) or path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError('音频路径或类型无效')
        return path

    def assign(self, kit_id, event, clips, options=None):
        if event not in SLOT_MAP or len(clips) > 64:
            raise ValueError('事件或切片数量无效')
        kit = self.load(kit_id)
        options = validate_event_options(options if options is not None else event_options(kit, event), len(clips))
        folder = self.directory(kit_id) / 'audio'
        folder.mkdir(exist_ok=True)
        assigned = []
        for item in clips:
            clip = validate_clip(item, event)
            original = Path(clip['path'])
            if not original.is_absolute():
                original = self.resolve(kit, clip)
            if not original.is_file() or original.suffix.lower() not in AUDIO_EXTENSIONS:
                raise ValueError('音频文件不存在或类型不支持')
            if original.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ValueError('单个音频文件不能超过 512 MiB')
            if original.resolve().is_relative_to(self.directory(kit_id).resolve()):
                destination = original
            else:
                digest = hashlib.sha256()
                with original.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                destination = folder / (digest.hexdigest()[:16] + original.suffix.lower())
                if not destination.exists():
                    shutil.copy2(original, destination)
            clip['path'] = destination.relative_to(self.directory(kit_id)).as_posix()
            assigned.append(clip)
        kit['tracks'][event] = assigned
        kit['event_options'][event] = options
        self._save(kit)
        return kit

    def delete(self, kit_id):
        folder = self.directory(kit_id)
        if folder.exists():
            shutil.rmtree(folder)

    def export(self, kit_id, output):
        kit = self.load(kit_id)
        files = {self.resolve(kit, clip) for clips in kit['tracks'].values() for clip in clips}
        if any(not path.is_file() for path in files):
            raise ValueError('部分切片文件已丢失，请重新选择后导出')
        if sum(path.stat().st_size for path in files) > MAX_ARCHIVE_BYTES:
            raise ValueError('音乐包总大小不能超过 512 MiB')
        public = copy.deepcopy(kit)
        public.pop('id', None)
        output = Path(output)
        if output.resolve().is_relative_to(self.directory(kit_id).resolve()):
            raise ValueError('请将音乐包导出到音乐盒目录之外')
        temporary = output.with_name(output.name + '.tmp')
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                archive.writestr('kit.json', json.dumps(public, ensure_ascii=False, indent=2))
                for path in sorted(files):
                    archive.write(path, path.relative_to(self.directory(kit_id)).as_posix())
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)

    def import_zip(self, filename):
        kit_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix='.import-', dir=self.root) as temporary:
            stage = Path(temporary)
            with zipfile.ZipFile(filename) as archive:
                entries = archive.infolist()
                if len(entries) > 512 or sum(i.file_size for i in entries) > MAX_ARCHIVE_BYTES:
                    raise ValueError('音乐包文件过多或解压后超过 512 MiB')
                names = set()
                for info in entries:
                    relative = safe_relative(info.filename)
                    if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                        raise ValueError('音乐包不能包含符号链接或加密文件')
                    if str(relative).casefold() in names:
                        raise ValueError('音乐包存在重复文件名')
                    names.add(str(relative).casefold())
                    if not info.is_dir():
                        path = stage / str(relative)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info) as src, path.open('wb') as dst:
                            shutil.copyfileobj(src, dst)
            manifests = [path for path in stage.rglob('kit.json') if path.is_file()]
            if len(manifests) != 1 or manifests[0].stat().st_size > 1024 * 1024:
                raise ValueError('音乐包需要一个有效的 kit.json 清单')
            manifest = manifests[0]
            data = json.loads(manifest.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict):
                raise ValueError('音乐包清单无效')
            name = str(data.get('name') or data.get('Name') or Path(filename).stem)[:100]
            kit = {'id': kit_id, 'name': name, 'format': 'cs2toolkit.music_kit', 'version': 1,
                   'tracks': {}, 'event_options': {}}
            options = data.get('event_options', {})
            if not isinstance(options, dict):
                raise ValueError('事件播放设置格式不正确')
            for slot in SLOTS:
                if isinstance(data.get('tracks'), dict):
                    raw = data['tracks'].get(slot.key, [])
                else:
                    raw = [data[slot.legacy]] if slot.legacy and isinstance(data.get(slot.legacy), dict) else []
                    extras = (data.get(slot.legacy + 's') or []) if slot.legacy else []
                    if isinstance(extras, list):
                        raw += extras
                    raw = [{'path': p.get('Path', ''), 'start': p.get('Start', 0),
                            'volume': p.get('Volume', 100), 'duration': slot.duration, 'loop': slot.loop}
                           for p in raw if isinstance(p, dict) and p.get('Path')]
                if not isinstance(raw, list) or len(raw) > 64:
                    raise ValueError('切片格式或数量无效')
                kit['event_options'][slot.key] = validate_event_options(options.get(slot.key), len(raw))
                kit['tracks'][slot.key] = []
                for value in raw:
                    clip = validate_clip(value, slot.key)
                    path = (manifest.parent / str(safe_relative(clip['path']))).resolve()
                    if not path.is_relative_to(stage.resolve()) or not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                        raise ValueError('音乐包缺少引用的音频文件')
                    clip['path'] = path.relative_to(stage).as_posix()
                    kit['tracks'][slot.key].append(clip)
            # Commit a validated package without retaining unrelated archive files.
            destination = self.directory(kit_id)
            destination.mkdir()
            try:
                used = {clip['path'] for clips in kit['tracks'].values() for clip in clips}
                for relative in used:
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(stage / relative, target)
                self._save(kit)
            except Exception:
                shutil.rmtree(destination)
                raise
            return kit
