"""Qt desktop frontend; all game operations remain CLI-only."""
import json
import sys
import queue
import threading
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QGridLayout, QLabel, QPushButton, QLineEdit, QSpinBox,
    QTabWidget, QScrollArea, QFrame, QListWidget, QPlainTextEdit,
    QComboBox, QFileDialog, QSplitter, QCheckBox)
from app import (Engine, CLI, Halt, ROOT, DEFAULT_CLI, SETTINGS, recipe_groups,
                 parse_targets, log_event, log_exception)
from recovery import send_game_escape
from facilities import ORDER, facility_for, ordered_names
from target_modes import freeze_targets, matches_pending
from dungeon_tab import DungeonPanel

STYLE_TEMPLATE = '''
QWidget { background:$bg; color:$fg; font-family:'Malgun Gothic'; font-size:13px; }
QLabel#title {font-size:28px; font-weight:bold; color:$title;}
QLabel#muted {color:$muted;}
QLabel#accent {color:$accent; font-weight:bold;}
QFrame#card {background:$card; border:1px solid $card_border; border-radius:12px;}
QFrame#card QLabel {background:transparent;}
QLineEdit,QSpinBox,QComboBox,QPlainTextEdit,QListWidget,QTableWidget {background:$input; border:1px solid $input_border; border-radius:6px; padding:7px;}
QTableWidget {gridline-color:$card_border; padding:2px;}
QHeaderView::section {background:$tab; color:$fg; border:0; border-bottom:1px solid $card_border; padding:4px 6px;}
QPushButton {background:$button; border:1px solid $button_border; border-radius:7px; padding:8px 12px;}
QPushButton:hover {background:$hover;}
QPushButton:disabled {color:$disabled_fg; background:$disabled_bg;}
QPushButton#primary {background:$accent; color:$accent_fg; font-weight:bold;}
QTabWidget::pane {border:0;}
QTabBar::tab {padding:12px 18px; background:$tab; color:$muted;}
QTabBar::tab:selected {color:$accent; border-bottom:2px solid $accent;}
QScrollArea {border:0;}
QSpinBox {min-height:25px;}
'''
# 공방 화면 색. 맨 위 「다크 모드」로 바꾸고 settings.json의 theme에 남는다.
THEMES = {
    'dark': dict(bg='#101720', fg='#e6edf5', title='#f1e6cc', muted='#91a3b8', card='#1b2634', card_border='#304054',
                 input='#151f2c', input_border='#35465b', button='#26394b', button_border='#40546a', hover='#36536a',
                 disabled_fg='#64748b', disabled_bg='#18222d', accent='#85cbbb', accent_fg='#102b29', tab='#151f2c'),
    'light': dict(bg='#f4f6f9', fg='#1f2933', title='#5a4520', muted='#52606d', card='#ffffff', card_border='#d3dae3',
                  input='#ffffff', input_border='#b8c2cc', button='#e4e9ef', button_border='#b8c2cc', hover='#d2dbe5',
                  disabled_fg='#9aa5b1', disabled_bg='#eceff3', accent='#1f7a68', accent_fg='#ffffff', tab='#e8edf2'),
}


def style_for(theme):
    style = STYLE_TEMPLATE
    for key, value in sorted(THEMES.get(theme, THEMES['light']).items(), key=lambda kv: -len(kv[0])):
        style = style.replace('$' + key, value)  # 긴 이름부터(button_border가 button보다 먼저)
    return style


STYLE = style_for('dark')


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('에린 생활 공방')
        self.setWindowIcon(QIcon(str(ROOT / 'assets' / 'workshop.ico')))
        self.resize(1240, 900)
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.running = False
        self.loading = False
        self.cli = None
        self.resume_event = threading.Event()
        self.recovery_attempts = 0
        self.engine = None
        self.targets = {}
        self.catalog = {'gather': [], 'alter': [], 'craft': []}
        self.groups = {}
        self.page = 0
        self.restoring = True
        try:
            self.saved = json.loads(SETTINGS.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.saved = {}
        self.defaults = self.saved.get('defaults', {})
        self.icons = self.saved.get('icons', {})
        self.pending = self.saved.get('pending', {})
        self.presets = self.saved.get('presets', {})
        self.ui_state = self.saved.get('ui', {})
        self.card_quantities = dict(self.ui_state.get('card_quantities', {}))
        self.facility_choices = dict(self.ui_state.get('facilities', {}))
        self.selected_group = self.ui_state.get('group', '')
        self.catalog_ready = False
        self.autosave = QTimer(self)
        self.autosave.setSingleShot(True)
        self.autosave.setInterval(300)
        self.autosave.timeout.connect(self.save)
        # 맨 위 탭: 생활(기존 화면)과 던전(dungeon/macro.py). 던전 탭은 처음 열 때 필요한 패키지를 설치한다.
        self.modes = QTabWidget()
        self.setCentralWidget(self.modes)
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 16, 0)
        self.dark = QCheckBox('다크 모드')
        self.dark.setChecked(self.saved.get('theme', 'light') == 'dark')
        self.dark.toggled.connect(self.theme_changed)
        corner_layout.addWidget(self.dark)
        self.modes.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        self.apply_theme()
        base = QWidget()
        # 생활 화면은 세로로 744px 넘게 쌓여서, 스크롤로 감싸 창을 그보다 작게 줄일 수 있게 한다.
        life = QScrollArea()
        life.setWidgetResizable(True)
        life.setWidget(base)
        self.modes.addTab(life, '생활')
        self.dungeon = DungeonPanel(self.saved.get('dungeon', {}), self.schedule_save, self.save,
                                    lambda: self.path.text(), can_launch=lambda: not self.running)
        self.modes.addTab(self.dungeon, '던전')
        layout = QVBoxLayout(base)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel('에린 생활 공방')
        title.setObjectName('title')
        layout.addWidget(title)
        subtitle = QLabel('채집부터 완성까지 · 최종 보유량 또는 추가 확보량을 설정하세요')
        subtitle.setObjectName('muted')
        layout.addWidget(subtitle)
        self.status = QLabel('게임 품목을 불러오는 중…')
        layout.addWidget(self.status)
        split = QSplitter()
        layout.addWidget(split, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.tabs = QTabWidget()
        for text in ('채집 재료', '가공 재료', '제작 아이템', '전체', '설정'):
            self.tabs.addTab(QWidget(), text)
        left_layout.addWidget(self.tabs)
        self.search = QLineEdit()
        self.search.setPlaceholderText('아이템 검색…')
        left_layout.addWidget(self.search)
        self.facility = QComboBox()
        self.facility.addItem('모든 시설')
        left_layout.addWidget(self.facility)
        self.facility.currentTextChanged.connect(self.facility_changed)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.card_host = QWidget()
        self.grid = QGridLayout(self.card_host)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.card_host)
        left_layout.addWidget(self.scroll, 1)
        nav = QHBoxLayout()
        prev = QPushButton('이전')
        nxt = QPushButton('다음')
        prev.clicked.connect(lambda: self.move_page(-1))
        nxt.clicked.connect(lambda: self.move_page(1))
        self.page_label = QLabel()
        nav.addWidget(prev)
        nav.addWidget(self.page_label)
        nav.addWidget(nxt)
        left_layout.addLayout(nav)
        self.settings_panel = self.make_settings()
        left_layout.addWidget(self.settings_panel)
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(QLabel('원클릭 프리셋 · 누르면 즉시 시작'))
        preset_scroll = QScrollArea()
        preset_scroll.setWidgetResizable(True)
        preset_scroll.setMaximumHeight(130)
        preset_host = QWidget()
        self.preset_layout = QVBoxLayout(preset_host)
        preset_scroll.setWidget(preset_host)
        rl.addWidget(preset_scroll)
        self.preset_name = QLineEdit()
        self.preset_name.setPlaceholderText('프리셋 이름 (예: 스노우 오브 준비)')
        rl.addWidget(self.preset_name)
        preset_save = QPushButton('현재 목표 저장 / 같은 이름 덮어쓰기')
        preset_save.clicked.connect(self.save_preset)
        rl.addWidget(preset_save)
        rl.addWidget(QLabel('목표 목록'))
        self.target_list = QListWidget()
        rl.addWidget(self.target_list, 1)
        remove = QPushButton('선택 목표 삭제')
        remove.clicked.connect(self.remove_target)
        rl.addWidget(remove)
        self.text = QLineEdit()
        self.text.setPlaceholderText('목재 30, 최상급 거미줄 1000개')
        rl.addWidget(self.text)
        add = QPushButton('텍스트 목표 추가 / 변경')
        add.clicked.connect(self.add_text)
        rl.addWidget(add)
        self.include_inventory = QCheckBox('가방에 있는 수량 포함')
        self.include_inventory.setChecked(self.saved.get('include_inventory', True))
        self.include_inventory.setToolTip('켜짐: 최종 보유량 / 꺼짐: 시작 시 가방 수량에 입력 수량만큼 추가')
        rl.addWidget(self.include_inventory)
        self.quantity_hint = QLabel()
        self.quantity_hint.setWordWrap(True)
        rl.addWidget(self.quantity_hint)
        self.include_inventory.toggled.connect(self.update_quantity_hint)
        self.update_quantity_hint()
        rl.addWidget(QLabel('정령의 날개 사용 한도'))
        self.budget = QSpinBox()
        self.budget.setRange(5, 1000000)
        self.budget.setSingleStep(5)
        self.budget.setValue(100)
        rl.addWidget(self.budget)
        self.recovery_mode = QComboBox()
        self.recovery_mode.addItems(['차단 시 대기 → 이어하기', '차단 시 ESC 복구 → 자동 이어하기'])
        rl.addWidget(self.recovery_mode)
        self.resume_button = QPushButton('화면 정리 완료 · 이어하기')
        self.resume_button.setEnabled(False)
        self.resume_button.clicked.connect(self.resume_event.set)
        rl.addWidget(self.resume_button)
        self.start = QPushButton('▶ 시작 / 재개')
        self.start.setObjectName('primary')
        self.start.clicked.connect(self.launch)
        rl.addWidget(self.start)
        stop = QPushButton('중지 요청')
        stop.clicked.connect(self.cancel)
        rl.addWidget(stop)
        hint = QLabel('가진 재료로 가공을 시작하고 부족분을 채집합니다.\n시설 완료 시 채집을 멈추고 수령합니다.\n등록된 가공은 중지 후에도 계속됩니다.')
        hint.setObjectName('muted')
        rl.addWidget(hint)
        split.addWidget(right)
        split.setSizes([850, 340])
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(170)
        layout.addWidget(self.log)
        self.tabs.currentChanged.connect(self.update_facilities)
        self.search.textChanged.connect(self.reset_page)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(150)
        self.reset_page()
        self.render_presets()
        for name, count in self.ui_state.get('targets', self.pending.get('targets', {})).items():
            self.add_target(name, count)
        if self.pending and not self.ui_state:
            self.include_inventory.setChecked(self.pending.get('include_inventory', True))
            self.budget.setValue(self.pending.get('budget', 100))
        if self.pending:
            self.message('이전 목표를 복원했습니다. 시작 시 현재 보유량부터 이어갑니다.')
        self.budget.setValue(self.ui_state.get('budget', self.budget.value()))
        self.include_inventory.setChecked(self.ui_state.get('include_inventory', self.include_inventory.isChecked()))
        self.recovery_mode.setCurrentIndex(self.ui_state.get('recovery_mode', 0))
        self.text.setText(self.ui_state.get('text', ''))
        self.preset_name.setText(self.ui_state.get('preset_name', ''))
        self.search.setText(self.ui_state.get('search', ''))
        self.tabs.setCurrentIndex(self.ui_state.get('tab', 0))
        for widget in (self.text, self.preset_name, self.search, self.path):
            widget.textChanged.connect(self.schedule_save)
        self.budget.valueChanged.connect(self.schedule_save)
        self.include_inventory.toggled.connect(self.schedule_save)
        self.recovery_mode.currentIndexChanged.connect(self.schedule_save)
        self.tabs.currentChanged.connect(self.schedule_save)
        self.route.currentIndexChanged.connect(self.route_changed)
        self.modes.currentChanged.connect(self.mode_changed)
        self.modes.setCurrentIndex(self.ui_state.get('mode', 0))
        self.restoring = False
        QTimer.singleShot(100, self.refresh)

    def make_settings(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        self.path = QLineEdit(self.saved.get('cli', DEFAULT_CLI))
        lay.addWidget(QLabel('게임 CLI 경로'))
        lay.addWidget(self.path)
        refresh = QPushButton('게임 품목 새로고침')
        refresh.clicked.connect(self.refresh)
        lay.addWidget(refresh)
        lay.addWidget(QLabel('기본 레시피 · 하위 재료 확보에도 적용'))
        self.group = QComboBox()
        self.route = QComboBox()
        self.group.currentTextChanged.connect(self.select_group)
        lay.addWidget(self.group)
        lay.addWidget(self.route)
        save = QPushButton('기본 경로 저장')
        save.clicked.connect(self.save_route)
        lay.addWidget(save)
        note = QLabel('아이템 이미지는 카드의 “이미지 연결”에서 지정할 수 있습니다.\n게임 패키지의 아이콘 자동 추출·이름 연결은 아직 지원되지 않습니다.')
        note.setWordWrap(True)
        lay.addWidget(note)
        return panel

    def apply_theme(self):
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(style_for('dark' if self.dark.isChecked() else 'light'))

    def theme_changed(self, *_):
        self.apply_theme()
        self.schedule_save()

    def mode_changed(self, index):
        if self.modes.widget(index) is self.dungeon:
            self.dungeon.activate()
        self.schedule_save()

    def update_quantity_hint(self):
        self.quantity_hint.setText('포함: 100개 보유 중 40개 입력 → 이미 달성'
                                   if self.include_inventory.isChecked()
                                   else '제외: 100개 보유 중 40개 입력 → 140개까지 확보')

    def save(self):
        if self.restoring:
            return True
        self.autosave.stop()
        self.ui_state = {
            'targets': dict(self.targets), 'text': self.text.text(),
            'preset_name': self.preset_name.text(), 'budget': self.budget.value(),
            'include_inventory': self.include_inventory.isChecked(),
            'recovery_mode': self.recovery_mode.currentIndex(),
            'tab': self.tabs.currentIndex(), 'search': self.search.text(),
            'facilities': dict(self.facility_choices), 'group': self.selected_group,
            'card_quantities': dict(self.card_quantities), 'mode': self.modes.currentIndex(),
        }
        self.saved.update(cli=self.path.text(), defaults=self.defaults, icons=self.icons, presets=self.presets,
                          include_inventory=self.include_inventory.isChecked(), ui=self.ui_state,
                          dungeon=self.dungeon.state(), theme='dark' if self.dark.isChecked() else 'light')
        try:
            temp = SETTINGS.with_suffix('.tmp')
            temp.write_text(json.dumps(self.saved, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(SETTINGS)
            return True
        except OSError as e:
            self.message(f'설정 저장 실패: {e}')
            return False

    def schedule_save(self, *_):
        if not self.restoring:
            self.autosave.start()

    def remember_quantity(self, name, count):
        self.card_quantities[name] = count
        self.schedule_save()

    def facility_changed(self, value):
        if not self.restoring and self.catalog_ready:
            self.facility_choices[str(self.tabs.currentIndex())] = value
            self.schedule_save()
        self.reset_page()

    def route_changed(self, *_):
        if not self.restoring and self.group.currentText():
            self.save_route()

    def render_presets(self):
        while self.preset_layout.count():
            item = self.preset_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for name, preset in self.presets.items():
            host = QWidget()
            row = QHBoxLayout(host)
            row.setContentsMargins(0, 0, 0, 0)
            run = QPushButton('▶ ' + name)
            run.setToolTip(', '.join(f'{n} {c}개' for n, c in preset['targets'].items())
                           + f'\n날개 한도 {preset["budget"]}'
                           + ('\n가방 포함 · 최종 보유량' if preset.get('include_inventory', True)
                              else '\n가방 제외 · 추가 확보량'))
            run.clicked.connect(lambda checked=False, n=name: self.run_preset(n))
            row.addWidget(run, 1)
            delete = QPushButton('삭제')
            delete.clicked.connect(lambda checked=False, n=name: self.delete_preset(n))
            row.addWidget(delete)
            self.preset_layout.addWidget(host)

    def save_preset(self):
        name = self.preset_name.text().strip()
        if not name or not self.targets:
            self.message('프리셋 이름과 목표 품목을 입력해주세요. 텍스트 목표는 먼저 추가해주세요.')
            return
        self.presets[name] = {'targets': dict(self.targets), 'budget': self.budget.value(),
                              'include_inventory': self.include_inventory.isChecked(),
                              'recovery_mode': self.recovery_mode.currentIndex(),
                              'defaults': {n: list(v) for n, v in self.defaults.items()}}
        if self.save():
            self.message(f'프리셋 저장: {name}')
        self.render_presets()

    def delete_preset(self, name):
        self.presets.pop(name, None)
        if self.save():
            self.message(f'프리셋 삭제: {name} · 목표 목록은 유지됩니다.')
        self.render_presets()

    def run_preset(self, name):
        if self.running:
            self.message('현재 작업을 마친 뒤 프리셋을 실행해주세요.')
            return
        preset = self.presets[name]
        self.targets = {}
        self.target_list.clear()
        self.text.clear()
        for item, count in preset['targets'].items():
            self.add_target(item, count)
        self.budget.setValue(preset['budget'])
        self.include_inventory.setChecked(preset.get('include_inventory', True))
        self.recovery_mode.setCurrentIndex(preset.get('recovery_mode', 0))
        self.defaults = {n: list(v) for n, v in preset.get('defaults', self.defaults).items()}
        self.select_group()
        self.preset_name.setText(name)
        self.render()
        self.message(f'프리셋 실행: {name}')
        self.launch()

    def select_group(self):
        self.route.blockSignals(True)
        self.route.clear()
        self.route.addItem('미지정', None)
        name = self.group.currentText()
        if name:
            self.selected_group = name
        for kind, exact in self.groups.get(name, []):
            self.route.addItem(('가공' if kind == 'alter' else '제작') + ' · ' + exact, [kind, exact])
        for i in range(self.route.count()):
            if self.route.itemData(i) == self.defaults.get(name):
                self.route.setCurrentIndex(i)
        self.route.blockSignals(False)
        self.schedule_save()

    def save_route(self):
        name = self.group.currentText()
        if not name:
            return
        value = self.route.currentData()
        if value:
            self.defaults[name] = value
        else:
            self.defaults.pop(name, None)
        self.save()
        self.message(f'{name}: 기본 경로 저장됨')

    def refresh(self):
        if self.running or self.loading:
            return
        self.loading = True
        path = self.path.text()
        def task():
            try:
                e = Engine(CLI(path), self.events.put, threading.Event())
                e.load()
                self.events.put(('catalog', e))
            except Exception as ex:
                log_exception('gui.catalog_error', ex, path=path)
                self.events.put(f'품목 조회 실패: {ex}')
            finally:
                self.events.put(('loaded', None))
        threading.Thread(target=task, daemon=True).start()

    def reset_page(self, *_):
        self.page = 0
        self.render()

    def update_facilities(self, *_):
        index = self.tabs.currentIndex()
        kinds = ('gather', 'alter', 'craft') if index == 3 else (('gather', 'alter', 'craft')[index],) if index < 3 else ()
        present = {facility_for(k, n) for k in kinds for n in self.catalog[k]}
        self.facility.blockSignals(True)
        self.facility.clear()
        self.facility.addItem('모든 시설')
        self.facility.addItems([n for n in ['채집'] + ORDER + ['미분류'] if n in present])
        selected = self.facility.findText(self.facility_choices.get(str(index), '모든 시설'))
        if selected >= 0:
            self.facility.setCurrentIndex(selected)
        self.facility.blockSignals(False)
        self.reset_page()

    def move_page(self, delta):
        self.page = max(0, self.page + delta)
        self.render()

    def render(self):
        settings = self.tabs.currentIndex() == 4
        self.settings_panel.setVisible(settings)
        self.scroll.setVisible(not settings)
        self.search.setVisible(not settings)
        self.facility.setVisible(not settings and self.tabs.currentIndex() != 0)
        if settings:
            self.page_label.setText('설정')
            return
        if self.tabs.currentIndex() == 3:
            pairs = [(k, n) for k in ('gather', 'alter', 'craft') for n in ordered_names(k, self.catalog[k])]
        else:
            kind = ('gather', 'alter', 'craft')[self.tabs.currentIndex()]
            pairs = [(kind, n) for n in ordered_names(kind, self.catalog[kind])]
        selected = self.facility.currentText()
        source = list(dict.fromkeys(n for k, n in pairs if selected in ('', '모든 시설') or facility_for(k, n) == selected))
        names = [n for n in source if self.search.text().casefold() in n.casefold()]
        pages = max(1, (len(names) + 11) // 12)
        self.page = min(self.page, pages - 1)
        self.page_label.setText(f'{len(names)}개 품목 · {self.page + 1} / {pages}')
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, name in enumerate(names[self.page * 12:(self.page + 1) * 12]):
            card = QFrame()
            card.setObjectName('card')
            cl = QVBoxLayout(card)
            icon = QLabel()
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pix = QPixmap(self.icons.get(name, ''))
            if pix.isNull():
                icon.setText(name[:2])
                icon.setObjectName('accent')  # 색은 테마를 따른다
                icon.setStyleSheet('font-size:26px; padding:8px;')
            else:
                icon.setPixmap(pix.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            cl.addWidget(icon)
            label = QLabel(name)
            label.setWordWrap(True)
            cl.addWidget(label)
            qty = QSpinBox()
            qty.setRange(1, 1000000)
            qty.setValue(self.card_quantities.get(name, self.targets.get(name, 1)))
            qty.valueChanged.connect(lambda count, n=name: self.remember_quantity(n, count))
            cl.addWidget(qty)
            row = QHBoxLayout()
            add = QPushButton('담기')
            add.clicked.connect(lambda checked=False, n=name, q=qty: self.add_target(n, q.value()))
            row.addWidget(add)
            image = QPushButton('이미지 연결')
            image.clicked.connect(lambda checked=False, n=name: self.link_icon(n))
            row.addWidget(image)
            cl.addLayout(row)
            self.grid.addWidget(card, i // 3, i % 3)

    def link_icon(self, name):
        path, _ = QFileDialog.getOpenFileName(self, '아이템 이미지 선택', '', 'Images (*.png *.webp *.jpg)')
        if path:
            if QPixmap(path).isNull():
                self.message('읽을 수 없는 이미지입니다.')
                return
            self.icons[name] = path
            self.save()
            self.render()

    def add_target(self, name, count):
        if self.running:
            self.message('작업 중에는 목표를 변경할 수 없습니다.')
            return
        self.targets[name] = count
        if not self.restoring or name not in self.card_quantities:
            self.card_quantities[name] = count
        self.target_list.clear()
        for n, c in self.targets.items():
            self.target_list.addItem(f'{n}   ·   {c:,}개')
        self.schedule_save()

    def add_text(self):
        try:
            for n, c in parse_targets(self.text.text()).items():
                self.add_target(n, c)
        except Halt as e:
            self.message(str(e))

    def remove_target(self):
        row = self.target_list.currentRow()
        if row >= 0 and not self.running:
            del self.targets[list(self.targets)[row]]
            self.target_list.takeItem(row)
            self.schedule_save()

    def launch(self):
        if self.running:
            return
        if self.dungeon.process is not None:
            self.message('던전 매크로를 중지한 뒤 생활 작업을 시작하세요.')
            return
        if not self.targets:
            self.message('목표 품목을 먼저 담아주세요.')
            return
        self.running = True
        self.start.setEnabled(False)
        self.include_inventory.setEnabled(False)
        self.stop.clear()
        self.cli = CLI(self.path.text())
        targets, defaults, budget = dict(self.targets), dict(self.defaults), self.budget.value()
        include_inventory = self.include_inventory.isChecked()
        auto = self.recovery_mode.currentIndex() == 1
        self.recovery_attempts = 0
        self.resume_event.clear()
        def recover(data, stop):
            self.events.put(('checkpoint', self.engine.calls))
            self.events.put(f'화면 차단: {data.get("kind", "unknown")} · 획득한 재료는 유지됩니다.')
            # Never repeatedly inject keys into an unidentified modal.
            if auto and data.get('kind') == 'unknown_modal' and self.recovery_attempts < 2:
                self.recovery_attempts += 1
                if send_game_escape():
                    self.events.put('게임에 ESC 1회 전송 · 2초 후 현재 상태로 이어합니다.')
                    stop.wait(2)
                    return
            self.resume_event.clear()
            self.events.put(('paused', None))
            self.events.put('화면을 닫은 뒤 “화면 정리 완료 · 이어하기”를 누르세요.')
            while not self.resume_event.wait(0.2):
                if stop.is_set():
                    return
            self.events.put(('resumed', None))
        self.engine = Engine(self.cli, self.events.put, self.stop, budget, defaults, recover)
        self.engine.on_charge = lambda calls: self.events.put(('checkpoint', calls))
        resume = matches_pending(self.pending, targets, include_inventory)
        resolved = self.pending.get('resolved_targets') if resume else None
        if resume:
            self.engine.calls = self.pending.get('calls', 0)
        self.pending = {'targets': targets, 'budget': budget, 'calls': self.engine.calls,
                        'include_inventory': include_inventory}
        if resolved is not None:
            self.pending['resolved_targets'] = resolved
        self.saved['pending'] = self.pending
        if not self.save():
            self.running = False
            self.start.setEnabled(True)
            self.include_inventory.setEnabled(True)
            return
        def task():
            try:
                goals = resolved
                if goals is None:
                    if self.engine.read('capabilities').get('loading'):
                        raise Halt('게임에 입장한 뒤 다시 시작하세요.')
                    bag = {} if include_inventory else self.engine.stock()
                    goals = freeze_targets(targets, bag, include_inventory)
                    # Persist the fixed baseline before spending any resources.
                    # UI owns settings writes; acknowledge successful saving.
                    acknowledged, result = threading.Event(), {}
                    self.events.put(('prepared', (goals, acknowledged, result)))
                    while not acknowledged.wait(0.1):
                        self.engine.check()
                    if not result.get('saved'):
                        raise Halt('목표 수량 저장 실패: 작업을 시작하지 않았습니다.')
                self.events.put(('가방 포함 · 최종 목표: ' if include_inventory else '가방 제외 · 고정 목표: ')
                                + ', '.join(f'{n} {c}개' for n, c in goals.items()))
                self.engine.run(goals)
                log_event('INFO', 'gui.task_complete', run_id=self.engine.run_id,
                          goals=goals, calls=self.engine.calls)
                self.events.put(('completed', None))
            except Exception as e:
                log_exception('gui.task_error', e, run_id=getattr(self.engine, 'run_id', None),
                              targets=targets, resolved_targets=locals().get('goals'),
                              calls=getattr(self.engine, 'calls', None))
                self.events.put(f'중단: {e}')
            finally:
                self.events.put(('checkpoint', self.engine.calls))
                self.events.put(('finished', None))
        threading.Thread(target=task, daemon=True).start()

    def cancel(self):
        if not self.running or self.stop.is_set():
            return
        self.stop.set()
        def task():
            try:
                self.cli.call('stop_action')
            except Exception as e:
                self.events.put(f'중지 요청: {e}')
        threading.Thread(target=task, daemon=True).start()
        self.message('중지 요청됨 · 현재 CLI 응답을 기다립니다.')

    def message(self, text):
        self.status.setText(text[:180])
        self.log.appendPlainText(text)

    def poll(self):
        while not self.events.empty():
            event = self.events.get_nowait()
            if isinstance(event, str):
                self.message(event)
                continue
            tag, e = event
            if tag == 'prepared':
                goals, acknowledged, result = e
                self.pending['resolved_targets'] = goals
                self.saved['pending'] = self.pending
                result['saved'] = self.save()
                if not result['saved']:
                    self.pending.pop('resolved_targets', None)
                acknowledged.set()
            elif tag == 'checkpoint':
                if self.pending:
                    self.pending['calls'] = e
                    self.saved['pending'] = self.pending
                    self.save()
            elif tag == 'completed':
                self.pending = {}
                self.saved['pending'] = {}
                self.save()
            elif tag == 'paused':
                self.resume_button.setEnabled(True)
            elif tag == 'resumed':
                self.resume_button.setEnabled(False)
            elif tag == 'finished':
                self.running = False
                self.start.setEnabled(True)
                self.include_inventory.setEnabled(True)
                self.resume_button.setEnabled(False)
            elif tag == 'loaded':
                self.loading = False
            elif tag == 'catalog':
                self.catalog_ready = True
                self.catalog['gather'] = sorted(e.gather)
                for kind in ('alter', 'craft'):
                    self.catalog[kind] = [n for n, opts in e.recipes.items() if any(k == kind for k, _ in opts)]
                self.groups = {n: [v for v in opts if opts.count(v) == 1]
                               for n, opts in recipe_groups(e.recipes).items() if any(opts.count(v) == 1 for v in opts)}
                previous_group = self.selected_group
                self.group.blockSignals(True)
                self.group.clear()
                self.group.addItems(sorted(self.groups))
                selection = self.group.findText(previous_group)
                if selection >= 0:
                    self.group.setCurrentIndex(selection)
                self.group.blockSignals(False)
                self.select_group()
                self.update_facilities()
                self.message(f'준비 완료 · 채집 {len(e.gather)}종 · 기본 경로 설정 {len(self.groups)}종')

    def closeEvent(self, event):
        if self.dungeon.process == 'inside':
            # exe에서는 던전 매크로가 이 프로세스 안에서 돈다. 클릭 도중 끊기지 않게 먼저 멈춘다.
            self.dungeon.request_stop()
            self.message('던전 매크로를 멈추는 중입니다. 멈춘 뒤 다시 닫아 주세요.')
            event.ignore()
        elif self.running:
            self.cancel()
            event.ignore()
        else:
            for spin in self.findChildren(QSpinBox):
                spin.interpretText()
            self.save()
            event.accept()


if __name__ == '__main__':
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Erin.Workshop.Desktop')
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(ROOT / 'assets' / 'workshop.ico')))
    app.setStyle('Fusion')
    # 화면 색(밝은·어두운 테마)은 Window가 설정에 따라 정한다
    window = Window()
    window.show()
    sys.exit(app.exec())
