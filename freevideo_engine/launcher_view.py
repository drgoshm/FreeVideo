"""Native setup wizard: compact navigation, rounded cards and live model status.

Every size, gap and hue comes from `branding`. Copy stays short here: a card
states what it needs and nothing else, and the full explanation lives in the
dialog the user opens for it.
"""
import tkinter as tk
from tkinter import ttk

from . import branding as b
from .model_status import FAMILIES, NAMES
from .widgets import Card, PillButton, Progress, Radio, StepRail


class Choice(Card):
    """One of two mutually exclusive setups. The dot sits beside its title so
    the card stays one text block tall on a 480-pixel-high window."""

    def __init__(self, parent, app, value, title, detail):
        super().__init__(parent, takefocus=1, cursor='hand2')
        self.app, self.value, self.disabled, self.hovered = app, value, False, False
        head = ttk.Frame(self.body, style='Card.TFrame')
        head.pack(fill='x')
        self.mark = Radio(head)
        self.mark.pack(side='left', padx=(0, b.space(2)))
        app.label(head, *title, style='Heading.Card.TLabel').pack(side='left')
        label = app.label(self.body, *detail, style='Muted.Card.TLabel', wraplength=310)
        label.pack(fill='x', pady=(b.space(1), 0))
        self.body.bind('<Configure>', lambda e: label.configure(wraplength=max(100, e.width)), add='+')
        for widget in (self, self.body, head, self.mark, *self.body.winfo_children(), *head.winfo_children()):
            widget.bind('<Button-1>', self.choose)
            widget.bind('<Enter>', self.hover, add='+')
            widget.bind('<Leave>', self.leave, add='+')
        self.bind('<Return>', self.choose); self.bind('<space>', self.choose)
        self.bind('<FocusIn>', lambda _: self.state()); self.bind('<FocusOut>', lambda _: self.state())

    def choose(self, _=None):
        if not self.disabled:
            self.app.mode.set(self.value)
            self.focus_set()
        return 'break'

    def hover(self, _=None):
        self.hovered = True
        self.state()

    def leave(self, _=None):
        # Tk reports Leave when the pointer crosses onto a child of this card,
        # so confirm it actually left the card before dropping the highlight.
        x, y = self.winfo_pointerxy()
        if (self.winfo_rootx() <= x < self.winfo_rootx() + self.winfo_width()
                and self.winfo_rooty() <= y < self.winfo_rooty() + self.winfo_height()):
            return
        self.hovered = False
        self.state()

    def state(self, states=None):
        if states is not None:
            self.disabled = 'disabled' in states
        selected = self.app.mode.get() == self.value
        focused = self.focus_get() is self
        self.tint(b.ACCENT if selected or focused else b.HOVER if self.hovered and not self.disabled else b.BORDER)
        self.mark.select(selected, self.disabled)
        self.resize()
        return ('disabled',) if self.disabled else ()


class ModelCard(Card):
    """One model family. The state line names the phase; a single metadata
    line carries the numbers, so three cards stay one block tall."""

    STATES = {'waiting': b.BORDER, 'pending': b.ACCENT, 'downloading': b.ACCENT,
              'verifying': b.ACCENT, 'ready': b.SUCCESS, 'paused': b.WARNING}

    def __init__(self, parent, app, name):
        super().__init__(parent, padding=b.space(4), radius=b.RADIUS_MD)
        self.resize_timer = None
        self.bind('<Destroy>', self.cancel_resize, add='+')
        self.app, self.name = app, name
        self.title = app.label(self.body, *NAMES[name], style='Heading.Card.TLabel')
        self.title.pack(anchor='w')
        self.state = ttk.Label(self.body, style='Accent.Card.TLabel')
        self.state.pack(anchor='w', pady=(b.space(1), b.space(2)))
        self.bar = Progress(self.body, height=b.space(1) + 2); self.bar.pack(fill='x')
        self.detail = ttk.Label(self.body, style='Meta.Card.TLabel', justify='left')
        self.detail.pack(fill='x', pady=(b.space(2), 0))
        self.body.bind('<Configure>', self.wrap, add='+')

    def wrap(self, event):
        for label in (self.title, self.state, self.detail):
            label.configure(wraplength=max(100, event.width))
        # Wrapping changes the required height, so measure again once Tk has
        # applied it; otherwise a two-line title clips the line below it.
        self.cancel_resize()
        self.resize_timer = self.after_idle(self.finish_wrap)

    def finish_wrap(self):
        self.resize_timer = None
        self.resize()

    def cancel_resize(self, event=None):
        if event is not None and event.widget != self:
            return
        if self.resize_timer is not None:
            self.after_cancel(self.resize_timer)
            self.resize_timer = None

    def update(self, item, *, ready=False):
        t = self.app.t
        size = lambda n: ('%.1f GiB' % (n/2**30)) if n >= 2**30 else ('%.0f MiB' % (n/2**20))
        if not item or ready and item.get('state') != 'ready':
            self.state.configure(text=t('Ready locally', '本地已就绪') if ready else t('Waiting for scan', '等待扫描'),
                                 foreground=b.SUCCESS if ready else b.MUTED)
            self.detail.configure(text='' if ready else t('Scan first', '先查找已有模型'))
            self.bar.configure(maximum=1, value=1 if ready else 0, tint=b.SUCCESS if ready else None)
            self.tint(b.SUCCESS if ready else b.BORDER)
            return
        state = item['state']
        labels = {'waiting': ('To download', '待下载') if item['download_bytes'] else ('Found locally', '已在本地找到'),
                  'pending': ('Waiting for remaining files', '等待补齐剩余文件'),
                  'downloading': ('Downloading', '下载中'),
                  'verifying': ('Transfer complete · verifying (no re-download)', '传输完成 · 正在校验（不会重复下载）'),
                  'ready': ('Ready', '已就绪'), 'paused': ('Paused · files retained', '已暂停 · 文件已保留')}
        hue = self.STATES.get(state, b.ACCENT)
        self.state.configure(text=t(*labels[state]), foreground=b.MUTED if state == 'waiting' else hue)
        self.tint(hue)
        existing, total, done = item['existing_bytes'], item['download_bytes'], item['downloaded_bytes']
        # The state line already names the phase, so these are numbers only.
        parts = []
        if state == 'ready':
            parts.append(size(item['total_bytes']))
        else:
            if total:
                parts.append('%s / %s' % (size(done), size(total)))
            elif existing:
                parts.append(size(existing))
            if item.get('ready_files'):
                parts.append(t('%d / %d files', '%d / %d 个文件') % (item['ready_files'], item['files']))
            rate = item.get('bytes_per_second')
            if rate:
                parts.append('%.1f MiB/s' % (rate/2**20))
            elif state == 'waiting' and existing > item.get('verified_bytes', 0):
                parts.append(t('verify first', '待校验'))
            if state == 'verifying':
                parts.append(t('Checksum is running; the next file may already be downloading',
                               '正在校验完整性；下一个文件可能已经开始下载'))
        self.detail.configure(text=' · '.join(parts))
        maximum = item['total_bytes'] or 1
        self.bar.configure(maximum=maximum, value=maximum if state == 'ready' else
                           min(maximum, item.get('verified_bytes', 0)+done),
                           tint=b.WARNING if state == 'paused' else None)


def build(app):
    window, t = app.window, app.t
    from .windows_ux import window_size, work_area
    width, height = window_size(window)
    left, top, screen_width, screen_height = work_area(window)
    window.title('FreeVideo')
    window.geometry('%dx%d%+d%+d' % (width, height, left+(screen_width-width)//2, top+(screen_height-height)//2))
    # Tall enough for the pages whose content is fixed: both choice pages and
    # the review page. At 540 every one of them clipped its last line, review
    # by 34 px, and a wizard that hides part of what it asks you to accept is
    # not a wizard. Installing, paused and ready pages still scroll: live task
    # lists, a retained error and the install path have no bounded height.
    window.minsize(min(680, width), min(600, height))
    b.theme(window)
    shell = ttk.Frame(window, padding=(b.space(7), b.space(4))); shell.pack(fill='both', expand=True)
    header = ttk.Frame(shell); header.pack(fill='x', pady=(0, b.space(4)))
    # Sized so the three header actions still fit their labels at 680 px,
    # the narrowest window this wizard allows.
    app.logo = b.wordmark(header, 170); app.logo.pack(side='left')
    app.language_button = app.button(header, '中文', 'EN', app.language)
    app.language_button.configure(style='Quiet.TButton'); app.language_button.pack(side='right')
    app.option_button = app.button(header, 'Settings', '设置', app.show_options)
    app.option_button.configure(style='Quiet.TButton'); app.option_button.pack(side='right', padx=b.space(1))
    app.download_button = app.button(header, 'Download sources', '下载源', app.download_settings)
    app.download_button.configure(style='Quiet.TButton'); app.download_button.pack(side='right', padx=b.space(1))
    app.steps_frame = StepRail(shell, app, (('ComfyUI', 'ComfyUI'), ('Models', '模型')))
    app.steps_frame.pack(fill='x', pady=(0, b.space(3)))
    # A hairline, not ttk's two-pixel separator: the rail already divides.
    app.steps_separator = tk.Frame(shell, height=1, background=b.BORDER)
    app.steps_separator.pack(fill='x')
    footer = ttk.Frame(shell, padding=(0, b.space(2), 0, 0)); footer.pack(side='bottom', fill='x')
    app.consent = ttk.Checkbutton(footer, variable=app.accept, command=app.refresh)
    actions = app.action_row = ttk.Frame(footer); actions.pack(side='bottom', fill='x')
    app.back_button = app.button(actions, 'Back', '上一步', app.back)
    app.back_button.configure(style='Quiet.TButton'); app.back_button.pack(side='left')
    app.launcher_button = app.button(actions, 'Return to launcher', '返回启动器', app.show_launcher)
    app.launcher_button.configure(style='Quiet.TButton')
    app.primary = PillButton(actions, app.act)
    app.primary.pack(side='right')
    app.stop_button = app.button(actions, 'Pause installation', '暂停安装', app.controller.cancel)
    app.stop_button.configure(style='Quiet.TButton')
    app.stop_button.pack(side='right', padx=(0, b.space(2)))
    tools = ttk.Frame(footer); tools.pack(side='bottom', fill='x', pady=(0, b.space(1)))
    app.detail_button = app.button(tools, 'Details', '详情', app.show_details)
    app.detail_button.configure(style='Quiet.TButton'); app.detail_button.pack(side='left')
    app.copy_error_button = app.button(tools, 'Copy error', '复制错误', app.copy_error)
    app.copy_error_button.configure(style='Quiet.TButton')
    from . import __version__
    ttk.Label(tools, text=__version__, style='Meta.TLabel').pack(side='right')
    app.update_row = tools
    from .launcher_terminal import Terminal
    app.terminal = Terminal(shell, app, footer)
    app.terminal_button = app.button(tools, 'Terminal', '终端', app.terminal.toggle)
    app.terminal_button.configure(style='Quiet.TButton'); app.terminal_button.pack(side='left', padx=b.space(1))
    viewport = ttk.Frame(shell); viewport.pack(fill='both', expand=True, pady=(b.space(4), 0))
    app.canvas = canvas = tk.Canvas(viewport, background=b.BACKGROUND, highlightthickness=0, width=1)
    scrollbar = ttk.Scrollbar(viewport, command=canvas.yview, style='Wizard.Vertical.TScrollbar')
    scrollbar.pack(side='right', fill='y'); canvas.pack(side='left', fill='both', expand=True)
    def scroll_state(first, last):
        scrollbar.set(first, last)
        if float(first) == 0 and float(last) == 1:
            scrollbar.pack_forget()
        elif not scrollbar.winfo_manager():
            scrollbar.pack(side='right', fill='y', before=canvas)
    canvas.configure(yscrollcommand=scroll_state)
    app.content = content = ttk.Frame(canvas)
    app.layout_timer = None
    handle = canvas.create_window(0, 0, window=content, anchor='nw')
    def place_content(_=None):
        if app.layout_timer is not None:
            window.after_cancel(app.layout_timer)
            app.layout_timer = None
        width = max(1, canvas.winfo_width())
        canvas.itemconfigure(handle, width=width)
        # Centre a page that fits; a taller one starts at the top and scrolls.
        height, needed = canvas.winfo_height(), content.winfo_reqheight()
        top = max(0, (height - needed)//2)
        canvas.coords(handle, 0, top)
        # Anchor the region at zero. Taking it from bbox('all') starts it at
        # the centring offset, and yview_moveto(0) then scrolls that offset
        # away, so a page was centred or flush to the top depending on
        # whether it had been shown before.
        canvas.configure(scrollregion=(0, 0, width, max(height, top + needed)))
    app.place_content = place_content
    canvas.bind('<Configure>', place_content)
    content.bind('<Configure>', place_content)
    def wheel(event):
        if str(event.widget).startswith(str(content)) or event.widget == canvas:
            canvas.yview_scroll(-1 if getattr(event, 'num', 0) == 4 else 1 if getattr(event, 'num', 0) == 5 else -int(event.delta/120), 'units')
    for name in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
        window.bind(name, wheel, add='+')
    app.heading = ttk.Label(content, style='Title.TLabel', anchor='center')
    app.heading.pack(fill='x')
    app.status_label = ttk.Label(content, textvariable=app.status, style='Muted.TLabel',
                                 anchor='center', justify='center', wraplength=800)
    # Leave room for font-dependent wrapping at the declared minimum size.
    app.status_label.pack(fill='x', pady=(b.space(1), b.space(3)))
    content.bind('<Configure>', lambda e: app.status_label.configure(wraplength=max(100, e.width)), add='+')
    app.pages = [ttk.Frame(content) for _ in range(4)]
    app.inputs = app.pages[0]
    choices = ttk.Frame(app.inputs); choices.pack(fill='x', pady=(0, b.space(4)))
    choices.columnconfigure((0, 1), weight=1, uniform='choices')
    app.mode_buttons = []
    for index, (value, title, detail) in enumerate((
        ('existing', ('I have ComfyUI', '我已安装 ComfyUI'), ('Connect your installation.', '接入现有安装。')),
        ('new', ('Install ComfyUI for me', '帮我安装 ComfyUI'), ('ComfyUI, Python and FreeVideo together.', '一并准备 ComfyUI、Python 和 FreeVideo。')))):
        choice = Choice(choices, app, value, title, detail)
        choice.grid(row=0, column=index, sticky='nsew', padx=(0, b.space(2)) if index == 0 else (b.space(2), 0))
        app.mode_buttons.append(choice)
    folder = Card(app.inputs); folder.pack(fill='x')
    ttk.Label(folder.body, textvariable=app.folder_label, style='Heading.Card.TLabel').pack(anchor='w')
    row = ttk.Frame(folder.body, style='Card.TFrame'); row.pack(fill='x', pady=(b.space(3), b.space(2)))
    app.entry = ttk.Entry(row, textvariable=app.selected_folder(), style='Well.TEntry')
    app.entry.pack(side='left', fill='x', expand=True)
    app.browse_button = app.button(row, 'Browse…', '浏览…', lambda: app.browse(app.selected_folder()))
    app.browse_button.configure(style='Card.TButton')
    app.browse_button.pack(side='right', padx=(b.space(2), 0))
    ttk.Label(folder.body, textvariable=app.folder_hint, style='Meta.Card.TLabel', wraplength=760).pack(fill='x')
    models = Card(app.pages[1], padding=b.space(4)); models.pack(fill='x')
    methods = ttk.Frame(models.body, style='Card.TFrame'); methods.pack(fill='x')
    app.model_method_buttons = []
    for value, en, zh in (('auto', 'Automatic download', '自动下载'),
                          ('manual', 'Download myself', '手动下载'), ('reuse', 'Use existing models', '复用已有模型')):
        button = ttk.Radiobutton(methods, text=app.t(en, zh), value=value, variable=app.model_method)
        button.pack(side='left', padx=(0, b.space(3)))
        app.labels.append((button, en, zh)); app.model_method_buttons.append(button)
    app.model_help = ttk.Label(models.body, style='Meta.Card.TLabel', wraplength=660)
    app.model_help.pack(fill='x', pady=(b.space(2), b.space(1)))
    models.body.bind('<Configure>', lambda e: app.model_help.configure(wraplength=max(100, e.width)), add='+')
    app.model_source_row = ttk.Frame(models.body, style='Card.TFrame')
    app.label(app.model_source_row, 'Download source', '下载源', style='Meta.Card.TLabel').pack(side='left', padx=(0, 8))
    app.model_source = ttk.Combobox(app.model_source_row, state='readonly', width=18)
    app.model_source.pack(side='left'); app.model_source.bind('<<ComboboxSelected>>', app.select_model_source)
    app.token_link = app.button(app.model_source_row, 'Test speeds / settings', '测速／设置', app.download_settings)
    app.token_link.configure(style='Card.TButton'); app.token_link.pack(side='left', padx=8)
    app.model_source_status = ttk.Label(models.body, style='Meta.Card.TLabel', wraplength=660)
    app.model_source_status.pack(fill='x')
    app.model_components = ttk.Frame(models.body, style='Card.TFrame')
    app.model_components.pack(fill='x', pady=(b.space(3), 0))
    app.label(app.model_components, 'Download links by component', '按组件查看下载链接', style='Meta.Card.TLabel').pack(anchor='w', pady=(0, 6))
    component_row = ttk.Frame(app.model_components, style='Card.TFrame'); component_row.pack(fill='x')
    app.component_buttons = {}
    for component, en, zh in (('video', 'Video model ↗', '视频模型 ↗'),
                              ('encoder', 'Prompt model ↗', '提示词模型 ↗'), ('decoder', 'Decoders ↗', '解码器 ↗')):
        button = app.button(component_row, en, zh, lambda name=component: app.model_download_link(name))
        button.configure(style='Card.TButton'); button.pack(side='left', padx=(0, 8))
        app.component_buttons[component] = button
    app.model_reuse_button = app.button(models.body, 'Choose model folders…', '选择模型文件夹…',
                                       lambda: app.model_method.set('reuse'))
    app.model_reuse_button.configure(style='Card.TButton')
    app.model_library = library = ttk.Frame(models.body, style='Card.TFrame')
    app.model_list = tk.Listbox(library, height=2, selectmode='extended', background=b.BACKGROUND,
                               foreground=b.TEXT, selectbackground=b.ACCENT_SUBTLE, selectforeground=b.TEXT,
                               highlightthickness=0, relief='flat', borderwidth=0,
                               font=(b.FAMILY, b.BODY), activestyle='none', exportselection=False)
    app.model_list.pack(fill='x', ipady=b.space(1))
    row = ttk.Frame(library, style='Card.TFrame'); row.pack(fill='x', pady=(8, 0))
    app.add_model_button = app.button(row, '+ Add folder', '+ 添加目录', app.add_model_folder)
    app.add_model_button.configure(style='Card.TButton'); app.add_model_button.pack(side='left')
    app.remove_model_button = app.button(row, 'Remove selected', '移除所选', app.remove_model_folders)
    app.remove_model_button.configure(style='Card.TButton')
    app.remove_model_button.pack(side='left', padx=b.space(2))
    # The plan figures and the disclosure share the centred title axis.
    app.summary = ttk.Label(app.pages[2], style='Muted.TLabel', wraplength=800,
                            anchor='center', justify='center')
    app.summary.pack(fill='x', pady=(0, b.space(2)))
    grid = ttk.Frame(app.pages[2]); grid.pack(fill='x', pady=(0, b.space(3)))
    grid.columnconfigure((0, 1, 2), weight=1, uniform='models')
    app.model_cards = {}
    # Optional pack controls live in the Qt launcher; keep this legacy setup
    # grid limited to the families setup itself provisions.
    for index, name in enumerate(name for name in FAMILIES if name != 'image'):
        card = ModelCard(grid, app, name)
        card.grid(row=0, column=index, sticky='nsew',
                  padx=(0 if index == 0 else b.space(2), 0 if index == 2 else b.space(2)))
        app.model_cards[name] = card
    # The phase is the headline; stage counts and elapsed time are metadata.
    app.progress_card = Card(app.pages[2], padding=b.space(4)); app.progress_card.pack(fill='x')
    app.progress_box = app.progress_card.body
    ttk.Label(app.progress_box, textvariable=app.phase, style='ProgressTitle.Card.TLabel',
              anchor='center', wraplength=760).pack(fill='x')
    app.overall_bar = Progress(app.progress_box, height=b.space(2)); app.overall_bar.pack(fill='x', pady=(b.space(3), b.space(2)))
    counters = ttk.Frame(app.progress_box, style='Card.TFrame'); counters.pack(fill='x')
    ttk.Label(counters, textvariable=app.overall_text, style='Meta.Card.TLabel').pack(side='left')
    ttk.Label(counters, textvariable=app.meta, style='Meta.Card.TLabel').pack(side='right')
    app.bar = Progress(app.progress_box, height=b.space(1) - 1, subdued=True)
    app.bar.pack(fill='x', pady=(b.space(4), 0))
    current = ttk.Frame(app.progress_box, style='Card.TFrame'); current.pack(fill='x', pady=(b.space(2), 0))
    ttk.Label(current, textvariable=app.current_text, style='Meta.Card.TLabel').pack(side='left')
    ttk.Label(current, textvariable=app.details, style='Meta.Card.TLabel').pack(side='right')
    app.review_card = Card(app.pages[2], padding=b.space(4), radius=b.RADIUS_MD,
                           fill=b.DANGER_SUBTLE, body_style='Alert.Card.TFrame')
    app.review_card.tint(b.DANGER)
    app.review = tk.Text(app.review_card.body, height=3, wrap='word', relief='flat',
                         background=b.DANGER_SUBTLE, foreground=b.DANGER, highlightthickness=0,
                         borderwidth=0, font=(b.FAMILY, b.BODY), state='disabled')
    app.review.pack(fill='x')
    launch = Card(app.pages[3], padding=b.space(7)); launch.pack(fill='x', pady=(b.space(3), b.space(5)))
    badge = tk.Canvas(launch.body, width=44, height=44, bg=b.SURFACE, highlightthickness=0)
    badge.pack(pady=(0, b.space(4)))
    badge.create_oval(1, 1, 43, 43, fill=b.SUCCESS_SUBTLE, outline='')
    badge.create_line(14, 23, 20, 30, 31, 16, fill=b.SUCCESS, width=3, capstyle='round', joinstyle='round')
    app.label(launch.body, 'Ready to launch', '安装已就绪', style='Hero.Card.TLabel', anchor='center').pack(fill='x', pady=(0, b.space(4)))
    app.launch_paths = ttk.Label(launch.body, style='Meta.Card.TLabel', anchor='center', justify='center', wraplength=680)
    app.launch_paths.pack(fill='x', pady=(0, b.space(3)))
    app.label(launch.body, 'Already running ComfyUI? FreeVideo connects automatically.',
        'ComfyUI 已在运行？会自动连接。', style='Meta.Card.TLabel', anchor='center', justify='center', wraplength=680).pack(fill='x')
    app.browser_controls = ttk.Frame(launch.body, style='Card.TFrame')
    ttk.Entry(app.browser_controls, textvariable=app.browser_address, state='readonly', justify='center').pack(fill='x')
    browser_buttons = ttk.Frame(app.browser_controls, style='Card.TFrame'); browser_buttons.pack(pady=(8, 0))
    app.browser_button = app.button(browser_buttons, 'Open browser', '打开浏览器', app.open_browser)
    app.browser_button.pack(side='left', padx=4)
    app.button(browser_buttons, 'Copy address', '复制地址', app.copy_browser_address).pack(side='left', padx=4)
    shortcuts = ttk.Frame(launch.body, style='Card.TFrame'); shortcuts.pack(pady=(16, 0))
    app.shortcut_button = app.button(shortcuts, 'Create desktop shortcut', '创建桌面快捷方式', app.create_shortcut)
    app.shortcut_button.pack(side='left', padx=4)
    app.shortcut_status = ttk.Label(shortcuts, style='Meta.Card.TLabel'); app.shortcut_status.pack(side='left', padx=4)
    launch.body.bind('<Configure>', lambda e: app.launch_paths.configure(wraplength=max(100, e.width)))
    app.launch_error = ttk.Label(app.pages[3], style='Danger.TLabel', wraplength=760, justify='left')
    app.launch_error.pack(fill='x')
    app.engine_update_row = ttk.Frame(app.pages[3])
    app.label(app.engine_update_row, 'Engine update available', '引擎有可用更新').pack(side='left')
    app.engine_update_button = app.button(app.engine_update_row, 'Update', '更新', app.update_engine)
    app.engine_update_button.pack(side='right')
    app.pages[3].bind('<Configure>', lambda e: app.launch_error.configure(wraplength=max(100, e.width)))


def show_page(app):
    if app.wizard_step == 3:
        app.primary.configure(width=32)
        app.primary.pack(side='top', anchor='center', pady=(0, b.space(2)), before=app.back_button)
        app.back_button.pack_configure(side='top', anchor='center')
        app.steps_frame.pack_forget()
    else:
        app.primary.configure(width=0)
        app.primary.pack(side='right', pady=0)
        app.back_button.pack_configure(side='left', anchor='w')
    if app.wizard_step != 3 and not app.steps_frame.winfo_manager():
        app.steps_frame.pack(fill='x', pady=(0, b.space(3)), before=app.steps_separator)
    app.heading.pack_configure(fill='x')
    for index, page in enumerate(app.pages):
        if index == app.wizard_step:
            if not page.winfo_manager():
                page.pack(fill='x'); app.canvas.yview_moveto(0)
        else:
            page.pack_forget()
    app.steps_frame.show(min(app.wizard_step, 1))
    if app.layout_timer is not None:
        app.window.after_cancel(app.layout_timer)
    # The root owns this callback so its Destroy handler can cancel it before
    # Tk deletes the registered command, even when a page closes before idle.
    app.layout_timer = app.window.after_idle(app.place_content)
