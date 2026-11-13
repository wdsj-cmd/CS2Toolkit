"""Local music-kit editor and preview, sharing the application's GSI service."""
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import QFileDialog, QFormLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action, BodyLabel, CaptionLabel, ComboBox, DoubleSpinBox, DropDownPushButton,
    FluentIcon as FIF, InfoBar, ListWidget, MessageBox, MessageBoxBase,
    PrimaryPushButton, PushButton, RoundMenu, ScrollArea, SimpleCardWidget,
    Slider, SpinBox, SubtitleLabel, SwitchButton, TitleLabel,
)

from app.logic.music_box_events import SLOTS, SLOT_MAP
from app.logic.music_box_library import AUDIO_EXTENSIONS, validate_clip
from app.ui.fluent_dialogs import TextInputDialog


class JobSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class AssetJob(QRunnable):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.signals = JobSignals()

    def run(self):
        try:
            self.signals.done.emit(self.operation())
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class ClipDialog(MessageBoxBase):
    def __init__(self, controller, event, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.event = event
        self.clips = [dict(clip, path=str(controller.library.resolve(controller.kit, clip)))
                      for clip in controller.kit['tracks'].get(event, [])]
        self.current = -1
        self.loading = False
        self.viewLayout.addWidget(SubtitleLabel(SLOT_MAP[event].title, self))
        hint = BodyLabel('可添加多个备选切片，每次事件随机播放一个。音频会复制到工具的数据目录。', self)
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        self.list = ListWidget(self)
        self.list.setFixedHeight(126)
        self.viewLayout.addWidget(self.list)
        row = QHBoxLayout()
        self.add_button = PushButton(FIF.ADD, '添加音频', self)
        self.remove_button = PushButton(FIF.DELETE, '移除', self)
        self.preview_button = PushButton(FIF.PLAY, '试听此切片', self)
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)
        row.addStretch()
        row.addWidget(self.preview_button)
        self.viewLayout.addLayout(row)

        form = QFormLayout()
        form.setSpacing(12)
        self.start = DoubleSpinBox(self)
        self.start.setRange(0, 86400)
        self.start.setDecimals(2)
        self.start.setSuffix(' 秒')
        self.duration = DoubleSpinBox(self)
        self.duration.setRange(0, 3600)
        self.duration.setDecimals(2)
        self.duration.setSuffix(' 秒')
        self.volume = Slider(Qt.Horizontal, self)
        self.volume.setRange(0, 100)
        self.volume_value = CaptionLabel('100%', self)
        volume_row = QHBoxLayout()
        volume_row.addWidget(self.volume, 1)
        volume_row.addWidget(self.volume_value)
        self.loop = SwitchButton(self)
        self.loop.setOnText('循环此切片')
        self.loop.setOffText('播放一次')
        form.addRow('从音频的位置开始', self.start)
        form.addRow('播放时长（0 为完整剩余音频）', self.duration)
        form.addRow('切片音量', volume_row)
        form.addRow('循环', self.loop)
        self.viewLayout.addLayout(form)
        note = CaptionLabel('时长可自行修改；新的游戏事件会切换曲目。试听时不循环。', self)
        note.setWordWrap(True)
        self.viewLayout.addWidget(note)
        self.widget.setMinimumWidth(540)
        self.yesButton.setText('保存切片')
        self.cancelButton.setText('取消')
        self.add_button.clicked.connect(self._add)
        self.remove_button.clicked.connect(self._remove)
        self.preview_button.clicked.connect(self._preview)
        self.list.currentRowChanged.connect(self._select)
        self.start.valueChanged.connect(self._store)
        self.duration.valueChanged.connect(self._store)
        self.volume.valueChanged.connect(self._store)
        self.loop.checkedChanged.connect(self._store)
        self.finished.connect(lambda _: controller.end_preview())
        self._refresh()

    def _refresh(self, selected=0):
        self.current = -1
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems([clip.get('name') or Path(clip['path']).name for clip in self.clips])
        self.list.blockSignals(False)
        self.list.setCurrentRow(min(selected, len(self.clips) - 1))
        if not self.clips:
            self._select(-1)
        self.add_button.setEnabled(len(self.clips) < 64)

    def _select(self, index):
        self.current = index
        valid = 0 <= index < len(self.clips)
        for widget in (self.start, self.duration, self.volume, self.loop, self.remove_button, self.preview_button):
            widget.setEnabled(valid)
        if not valid:
            return
        self.loading = True
        clip = self.clips[index]
        self.start.setValue(clip['start'])
        self.duration.setValue(clip['duration'])
        self.volume.setValue(int(clip['volume']))
        self.volume_value.setText(f"{int(clip['volume'])}%")
        self.loop.setChecked(clip['loop'])
        self.loading = False

    def _store(self, *_):
        if self.loading or not 0 <= self.current < len(self.clips):
            return
        self.clips[self.current].update(start=self.start.value(), duration=self.duration.value(),
                                        volume=self.volume.value(), loop=self.loop.isChecked())
        self.volume_value.setText(f'{self.volume.value()}%')

    def _add(self):
        patterns = ' '.join('*' + ext for ext in sorted(AUDIO_EXTENSIONS))
        paths, _ = QFileDialog.getOpenFileNames(self, '选择音频切片', '', f'音频文件 ({patterns})')
        if not paths:
            return
        start = len(self.clips)
        for path in paths[:64 - start]:
            self.clips.append(validate_clip({'path': path, 'name': Path(path).name}, self.event))
        self._refresh(start)

    def _remove(self):
        if 0 <= self.current < len(self.clips):
            index = self.current
            self.controller.end_preview()
            self.clips.pop(index)
            self._refresh(index)

    def _preview(self):
        if 0 <= self.current < len(self.clips):
            self.controller.preview(self.event, self.clips[self.current])


class MusicBoxPage(ScrollArea):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.job = None
        self._completion = None
        self.view = QWidget(self)
        self.view.setObjectName('musicBoxView')
        self.layout = QVBoxLayout(self.view)
        self.layout.setContentsMargins(20, 14, 20, 24)
        self.layout.setSpacing(10)
        self.layout.setAlignment(Qt.AlignTop)
        self.setWidget(self.view)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet('QScrollArea {background: transparent; border: none;} #musicBoxView {background: transparent;}')
        self.layout.addWidget(TitleLabel('创意音乐盒', self))
        intro = BodyLabel('为游戏事件配置自己的音乐切片，支持独立音量、试听和音乐包分享。', self)
        intro.setWordWrap(True)
        self.layout.addWidget(intro)

        master = SimpleCardWidget(self)
        master_layout = QVBoxLayout(master)
        master_layout.setContentsMargins(16, 12, 16, 12)
        row = QHBoxLayout()
        row.addWidget(SubtitleLabel('启用创意音乐盒', master), 1)
        self.enable = SwitchButton(master)
        self.enable.setOnText('开')
        self.enable.setOffText('关')
        row.addWidget(self.enable)
        master_layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(BodyLabel('音乐音量', master))
        self.volume = Slider(Qt.Horizontal, master)
        self.volume.setRange(0, 100)
        row.addWidget(self.volume, 1)
        self.volume_value = CaptionLabel('50%', master)
        self.volume_value.setMinimumWidth(34)
        row.addWidget(self.volume_value)
        self.stop_button = PushButton('停止播放', master)
        row.addWidget(self.stop_button)
        master_layout.addLayout(row)
        self.status = CaptionLabel(controller.status, master)
        self.status.setWordWrap(True)
        master_layout.addWidget(self.status)
        self.layout.addWidget(master)

        row = QHBoxLayout()
        self.kits = ComboBox(self)
        self.kits.setMinimumWidth(160)
        row.addWidget(self.kits, 1)
        self.create_button = PrimaryPushButton('新建', self)
        self.import_button = PushButton('导入 ZIP', self)
        self.export_button = PushButton('导出', self)
        self.more = DropDownPushButton('管理', self)
        menu = RoundMenu(parent=self.more)
        menu.addAction(Action(FIF.EDIT, '重命名', triggered=self._rename))
        menu.addAction(Action(FIF.DELETE, '删除音乐盒', triggered=self._delete))
        self.more.setMenu(menu)
        for button in (self.create_button, self.import_button, self.export_button, self.more):
            row.addWidget(button)
        self.layout.addLayout(row)
        self.summary = CaptionLabel('', self)
        self.summary.setWordWrap(True)
        self.layout.addWidget(self.summary)

        timing = SimpleCardWidget(self)
        timing_layout = QVBoxLayout(timing)
        timing_layout.setContentsMargins(16, 12, 16, 12)
        row = QHBoxLayout()
        label = BodyLabel('缺少 GSI 倒计时时允许估算', timing)
        row.addWidget(label, 1)
        self.estimate = SwitchButton(timing)
        self.estimate.setOnText('开')
        self.estimate.setOffText('关')
        row.addWidget(self.estimate)
        timing_layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(CaptionLabel('C4 总计时', timing))
        self.bomb_seconds = SpinBox(timing)
        self.bomb_seconds.setRange(11, 180)
        self.bomb_seconds.setSuffix(' 秒')
        row.addWidget(self.bomb_seconds)
        row.addSpacing(8)
        row.addWidget(CaptionLabel('回合总计时', timing))
        self.round_seconds = SpinBox(timing)
        self.round_seconds.setRange(11, 3600)
        self.round_seconds.setSuffix(' 秒')
        row.addWidget(self.round_seconds)
        timing_layout.addLayout(row)
        hint = CaptionLabel('优先使用游戏倒计时。缺失时按服务器时长估算；修改时长从下一次回合或安包生效。', timing)
        hint.setWordWrap(True)
        timing_layout.addWidget(hint)
        self.layout.addWidget(timing)

        self.cards = {}
        for slot in SLOTS:
            card = SimpleCardWidget(self)
            layout = QHBoxLayout(card)
            layout.setContentsMargins(16, 12, 16, 12)
            text = QVBoxLayout()
            title = BodyLabel(slot.title, card)
            hint = CaptionLabel(slot.hint, card)
            hint.setWordWrap(True)
            count = CaptionLabel('未设置切片', card)
            text.addWidget(title)
            text.addWidget(hint)
            text.addWidget(count)
            layout.addLayout(text, 1)
            preview = PushButton('试听', card)
            edit = PushButton('设置', card)
            layout.addWidget(preview)
            layout.addWidget(edit)
            preview.clicked.connect(lambda checked=False, key=slot.key: controller.preview(key))
            edit.clicked.connect(lambda checked=False, key=slot.key: self._edit(key))
            self.cards[slot.key] = (count, preview, edit)
            self.layout.addWidget(card)

        note = CaptionLabel('首次使用请在设置中重新生成 GSI 配置并重启 CS2。若与游戏原音乐重叠，可在游戏中调低对应音乐音量。\n'
                            '中途接入的回合不估算已有计时；安包者信息缺失时使用通用切片。', self)
        note.setWordWrap(True)
        self.layout.addWidget(note)
        reference = BodyLabel('<a href="https://github.com/TYFALY/CS-Jukebox-V2">音乐事件设计参考 CS-Jukebox-V2</a>', self)
        reference.setOpenExternalLinks(True)
        self.layout.addWidget(reference)
        self.enable.checkedChanged.connect(controller.set_enabled)
        self.volume.valueChanged.connect(self._volume_changed)
        self.estimate.checkedChanged.connect(self._timing_changed)
        self.bomb_seconds.valueChanged.connect(self._timing_changed)
        self.round_seconds.valueChanged.connect(self._timing_changed)
        self.stop_button.clicked.connect(controller.stop)
        self.create_button.clicked.connect(self._create)
        self.import_button.clicked.connect(self._import)
        self.export_button.clicked.connect(self._export)
        self.kits.currentIndexChanged.connect(self._select)
        controller.changed.connect(self.refresh)
        controller.status_changed.connect(self.status.setText)
        controller.error.connect(self._error)
        self.refresh()

    def refresh(self):
        kit = self.controller.kit
        busy = self.job is not None
        self.enable.blockSignals(True)
        self.enable.setChecked(self.controller.enabled)
        self.enable.blockSignals(False)
        self.volume.blockSignals(True)
        self.volume.setValue(int(self.controller.settings['volume']))
        self.volume.blockSignals(False)
        self.volume_value.setText(f'{self.volume.value()}%')
        for widget, key in ((self.estimate, 'estimate_timers'), (self.bomb_seconds, 'bomb_seconds'), (self.round_seconds, 'round_seconds')):
            widget.blockSignals(True)
            if widget is self.estimate:
                widget.setChecked(bool(self.controller.settings[key]))
            else:
                widget.setValue(int(self.controller.settings[key]))
            widget.blockSignals(False)
        self.kits.blockSignals(True)
        self.kits.clear()
        kits = self.controller.library.list_kits()
        for item in kits:
            self.kits.addItem(item['name'], userData=item['id'])
        if kit:
            self.kits.setCurrentIndex(next((i for i, item in enumerate(kits) if item['id'] == kit['id']), -1))
        else:
            self.kits.setPlaceholderText('先新建或导入音乐盒')
        self.kits.blockSignals(False)
        count = sum(bool(clips) for clips in kit['tracks'].values()) if kit else 0
        self.summary.setText(f'已配置 {count} / {len(SLOTS)} 个事件；多个备选切片随机播放。' if kit
                             else '音乐盒还没有音频。点击“新建”开始配置，或导入已有音乐包。')
        for widget in (self.kits, self.create_button, self.import_button):
            widget.setEnabled(not busy)
        for widget in (self.export_button, self.more):
            widget.setEnabled(bool(kit) and not busy)
        for key, (label, preview, edit) in self.cards.items():
            clips = kit['tracks'].get(key, []) if kit else []
            label.setText(f'{len(clips)} 个切片' if clips else '未设置切片')
            preview.setEnabled(bool(clips) and not busy)
            edit.setEnabled(bool(kit) and not busy)

    def _volume_changed(self, value):
        self.controller.set_volume(value)
        self.volume_value.setText(f'{value}%')

    def _timing_changed(self, *_):
        self.controller.set_timing(self.estimate.isChecked(), self.bomb_seconds.value(), self.round_seconds.value())

    def _select(self, index):
        if index >= 0:
            self.controller.select_kit(self.kits.itemData(index))

    def _create(self):
        dialog = TextInputDialog('新建音乐盒', '音乐盒名称', '', self.window())
        if dialog.exec():
            try:
                kit = self.controller.library.create(dialog.get_text())
                self.controller.select_kit(kit['id'])
            except (OSError, ValueError) as exc:
                self._error(str(exc))
        dialog.deleteLater()

    def _rename(self):
        kit = self.controller.kit
        if not kit:
            return
        dialog = TextInputDialog('重命名音乐盒', '音乐盒名称', kit['name'], self.window())
        if dialog.exec():
            try:
                self.controller.library.rename(kit['id'], dialog.get_text())
                self.controller.reload_library()
            except (OSError, ValueError) as exc:
                self._error(str(exc))
        dialog.deleteLater()

    def _delete(self):
        kit = self.controller.kit
        if kit and MessageBox('删除音乐盒', f"确定删除“{kit['name']}”及其已复制的音频？", self.window()).exec():
            self.controller.stop()
            self._run_job(lambda: self.controller.library.delete(kit['id']), '音乐盒已删除')

    def _edit(self, event):
        if not self.controller.kit:
            return
        kit_id = self.controller.kit['id']
        dialog = ClipDialog(self.controller, event, self.window())
        if dialog.exec():
            clips = dialog.clips
            self.controller.stop()
            self._run_job(lambda: self.controller.library.assign(kit_id, event, clips), '切片已保存')
        dialog.deleteLater()

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, '导入音乐盒', '', '音乐包 (*.zip)')
        if path:
            self._run_job(lambda: self.controller.library.import_zip(path), '音乐盒已导入', select=True)

    def _export(self):
        kit = self.controller.kit
        if not kit:
            return
        path, _ = QFileDialog.getSaveFileName(self, '导出音乐盒', 'music-kit.zip', '音乐包 (*.zip)')
        if path:
            if not path.lower().endswith('.zip'):
                path += '.zip'
            self._run_job(lambda: self.controller.library.export(kit['id'], path), '音乐盒已导出')

    def _run_job(self, operation, message, select=False):
        if self.job is not None:
            return
        self._completion = (message, select)
        self.job = AssetJob(operation)
        self.job.signals.done.connect(self._job_done)
        self.job.signals.failed.connect(self._job_failed)
        self.status.setText('正在处理音乐文件…')
        self.refresh()
        QThreadPool.globalInstance().start(self.job)

    @Slot(object)
    def _job_done(self, result):
        message, select = self._completion
        self.job = None
        self.controller.reload_library()
        if select and isinstance(result, dict):
            self.controller.select_kit(result['id'])
        self.status.setText(message)
        InfoBar.success('创意音乐盒', message, parent=self.window(), duration=2500)

    @Slot(str)
    def _job_failed(self, message):
        self.job = None
        self.refresh()
        self._error(message)

    @Slot(str)
    def _error(self, message):
        self.status.setText(message)
        InfoBar.error('创意音乐盒', message, parent=self.window(), duration=7000)
