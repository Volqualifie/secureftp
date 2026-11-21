"""主窗口：远程文件浏览 + 传输列表 + 连接状态。

功能：浏览/进入目录、上传文件与文件夹（按钮或拖拽）、下载文件与文件夹、
多任务并发（超出并发数的排队）、暂停/继续（断点续传）、断网自动重连后续传、模拟断网（演示用）。
"""
import os
import posixpath

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSizePolicy, QSplitter, QStyle, QTableWidget, QTableWidgetItem, QToolBar,
    QVBoxLayout, QWidget,
)

from ..transfer import list_remote_tree
from .common import human_size, human_time, run_in_background
from .transfer_view import TransferTable
from .transfers import DONE, RUNNING, WAITING, TransferManager

STATE_TEXT = {
    "connected": ("● 已连接", "#1e8e3e"),
    "disconnected": ("● 连接断开", "#d93025"),
    "reconnecting": ("● 正在重连…", "#e37400"),
    "auth_failed": ("● 认证失败", "#d93025"),
    "closed": ("● 已关闭", "#777777"),
}


def normalize(path: str) -> str:
    p = posixpath.normpath(path or "")
    return "" if p in (".", "/") else p.lstrip("/")


class MainWindow(QMainWindow):
    state_changed = Signal(str, str)        # 网络线程 -> 界面线程

    def __init__(self, client, server_label="", max_parallel=3, parent=None):
        super().__init__(parent)
        self.client = client
        self.cwd = ""
        self._entries = []
        self._nav_seq = 0
        self._nav_done = 0                  # 最近一次完成（成功或失败）的导航序号
        self._conn_state = client.state
        self.setWindowTitle(f"安全文件传输 - {server_label}" if server_label else "安全文件传输")
        self.resize(1040, 720)
        self.setAcceptDrops(True)

        self.manager = TransferManager(client, max_parallel, self)
        self._build_ui(server_label)

        self.manager.task_added.connect(self._on_task_added)
        self.manager.task_updated.connect(self._on_task_updated)
        self.manager.task_removed.connect(self.transfers.remove_task)
        self.manager.task_done.connect(self._on_task_done)
        self.transfers.pause_requested.connect(self.manager.pause)
        self.transfers.resume_requested.connect(self.manager.resume)
        self.transfers.remove_requested.connect(self.manager.remove)
        self.state_changed.connect(self._on_state)
        client.on_state = lambda s, d: self.state_changed.emit(s, d)    # 在网络线程回调，信号自动排队

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(lambda: self.navigate(self.cwd))

        self._apply_state(client.state)
        self.navigate("")

    # ------------------------------------------------------------ 界面搭建
    def _icon(self, sp):
        return self.style().standardIcon(sp)

    def _build_ui(self, server_label):
        SP = QStyle.StandardPixmap
        tb = QToolBar("工具栏")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        def act(icon, text, slot, shortcut=None, tip=""):
            a = QAction(self._icon(icon), text, self)
            a.triggered.connect(lambda _=False: slot())
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setToolTip(tip or text)
            tb.addAction(a)
            return a

        self.act_up_files = act(SP.SP_ArrowUp, "上传文件", self.choose_upload_files, "Ctrl+U")
        self.act_up_dir = act(SP.SP_DirOpenIcon, "上传文件夹", self.choose_upload_dir)
        self.act_down = act(SP.SP_ArrowDown, "下载", self.download_selected, "Ctrl+D",
                            "下载选中的文件或文件夹")
        tb.addSeparator()
        self.act_parent = act(SP.SP_FileDialogToParent, "上一级", self.go_parent, "Backspace")
        self.act_refresh = act(SP.SP_BrowserReload, "刷新", lambda: self.navigate(self.cwd), "F5")
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.act_drop = act(SP.SP_BrowserStop, "模拟断网", self.simulate_drop, None,
                            "主动断开 TCP 连接，用于演示自动重连与断点续传")

        # 远程文件区
        self.path_edit = QLineEdit("/")
        self.path_edit.setReadOnly(True)
        self.files = QTableWidget(0, 3)
        self.files.setHorizontalHeaderLabels(["名称", "大小", "修改时间"])
        self.files.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.files.verticalHeader().setVisible(False)
        self.files.setShowGrid(False)
        self.files.setAlternatingRowColors(True)
        h = self.files.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.files.setColumnWidth(1, 110)
        self.files.setColumnWidth(2, 150)
        self.files.cellDoubleClicked.connect(self._on_double_click)

        self.hint = QLabel("提示：可把本地文件或文件夹直接拖到窗口里上传")
        self.hint.setStyleSheet("color: #777;")
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(8, 8, 8, 4)
        tl.addWidget(QLabel("远程目录"))
        tl.addWidget(self.path_edit)
        tl.addWidget(self.files, 1)
        tl.addWidget(self.hint)

        # 传输区
        self.transfers = TransferTable()
        self.btn_pause_all = QPushButton("全部暂停")
        self.btn_clear = QPushButton("清除已完成")
        self.btn_pause_all.clicked.connect(self.manager.pause_all)
        self.btn_clear.clicked.connect(self.manager.clear_finished)
        head = QHBoxLayout()
        head.addWidget(QLabel("传输列表"))
        head.addStretch(1)
        head.addWidget(self.btn_pause_all)
        head.addWidget(self.btn_clear)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(8, 4, 8, 8)
        bl.addLayout(head)
        bl.addWidget(self.transfers, 1)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(bottom)
        split.setSizes([360, 300])
        self.setCentralWidget(split)

        # 状态栏
        self.conn_label = QLabel()
        self.summary_label = QLabel("")
        user = f"{self.client.username or ''}" + ("  [VIP]" if self.client.is_vip else "")
        self.user_label = QLabel(user)
        sb = self.statusBar()
        sb.addWidget(self.conn_label)
        sb.addWidget(self.user_label)
        sb.addPermanentWidget(self.summary_label)

    # ------------------------------------------------------------ 连接状态
    def _apply_state(self, state):
        text, color = STATE_TEXT.get(state, (state, "#777"))
        self.conn_label.setText(text)
        self.conn_label.setStyleSheet(f"color: {color}; font-weight: bold; padding: 0 8px;")
        self.transfers.refresh_all(state == "connected")
        online = state == "connected"
        self.act_drop.setEnabled(online)

    def _on_state(self, state, detail):
        prev, self._conn_state = self._conn_state, state
        self._apply_state(state)
        if state == "connected" and prev in ("disconnected", "reconnecting"):
            self.statusBar().showMessage("已自动重连，正在刷新目录并续传未完成的任务", 5000)
            self.navigate(self.cwd)
        elif state == "auth_failed":
            QMessageBox.warning(self, "认证失败", "重连时登录失败，请重新启动程序登录。")

    def simulate_drop(self):
        self.statusBar().showMessage("已主动断开 TCP 连接，客户端将自动重连并续传", 5000)
        self.client.drop_connection()

    # ------------------------------------------------------------ 浏览
    def navigate(self, path):
        path = normalize(path)
        self._nav_seq += 1
        seq = self._nav_seq

        def ok(entries):
            if seq == self._nav_seq:            # 忽略过期的响应
                self._show(path, entries)
                self._nav_done = seq

        def err(e):
            if seq == self._nav_seq:
                self.statusBar().showMessage(f"读取目录失败：{e}", 8000)
                self._nav_done = seq

        run_in_background(lambda: self.client.catalog(path), ok, err)

    def _show(self, path, entries):
        self.cwd = path
        self._entries = entries
        self.path_edit.setText("/" + path)
        self.act_parent.setEnabled(path != "")
        SP = QStyle.StandardPixmap
        self.files.setRowCount(len(entries))
        for r, e in enumerate(entries):
            name = QTableWidgetItem(self._icon(SP.SP_DirIcon if e["is_dir"] else SP.SP_FileIcon),
                                    e["name"])
            size = QTableWidgetItem("" if e["is_dir"] else human_size(e["size"]))
            size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.files.setItem(r, 0, name)
            self.files.setItem(r, 1, size)
            self.files.setItem(r, 2, QTableWidgetItem(human_time(e["mtime"])))

    def _on_double_click(self, row, _col):
        if 0 <= row < len(self._entries) and self._entries[row]["is_dir"]:
            self.navigate(posixpath.join(self.cwd, self._entries[row]["name"]))

    def go_parent(self):
        if self.cwd:
            self.navigate(posixpath.dirname(self.cwd))

    def _schedule_refresh(self):
        self._refresh_timer.start(300)          # 合并短时间内的多次刷新请求

    def _selected_entries(self):
        rows = sorted({i.row() for i in self.files.selectedIndexes()})
        return [self._entries[r] for r in rows if r < len(self._entries)]

    # ------------------------------------------------------------ 上传
    def _confirm(self, title, text) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def choose_upload_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要上传的文件")
        if paths:
            self.upload_paths(paths)

    def choose_upload_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择要上传的文件夹")
        if d:
            self.upload_paths([d])

    def upload_paths(self, paths, confirm=True):
        """上传若干文件/文件夹到当前远程目录。文件夹会保留内部结构。返回创建的任务数。"""
        jobs = []                                   # (本地路径, 远程路径)
        for p in paths:
            p = os.path.abspath(p)
            if os.path.isdir(p):
                base = os.path.basename(p.rstrip("\\/"))
                for root, _dirs, files in os.walk(p):
                    for f in files:
                        full = os.path.join(root, f)
                        rel = os.path.relpath(full, p).replace(os.sep, "/")
                        jobs.append((full, posixpath.join(self.cwd, base, rel)))
            elif os.path.isfile(p):
                jobs.append((p, posixpath.join(self.cwd, os.path.basename(p))))
        if not jobs:
            self.statusBar().showMessage("没有可上传的文件", 4000)
            return 0
        existing = {e["name"] for e in self._entries}
        clash = [os.path.basename(l) for l, r in jobs if posixpath.dirname(r) == self.cwd
                 and posixpath.basename(r) in existing]
        if clash and confirm and not self._confirm(
                "覆盖确认", f"远程目录中已存在 {len(clash)} 个同名文件（如 {clash[0]}），是否覆盖？"):
            return 0
        for local, remote in jobs:
            self.manager.add_upload(local, remote)
        return len(jobs)

    # ------------------------------------------------------------ 下载
    def download_selected(self):
        entries = self._selected_entries()
        if not entries:
            self.statusBar().showMessage("请先在远程目录中选择要下载的文件或文件夹", 4000)
            return
        dest = QFileDialog.getExistingDirectory(self, "选择保存位置")
        if dest:
            self.download_entries(entries, dest)

    def download_entries(self, entries, dest, confirm=True):
        files = [e for e in entries if not e["is_dir"]]
        dirs = [e for e in entries if e["is_dir"]]
        clash = [e["name"] for e in files if os.path.exists(os.path.join(dest, e["name"]))]
        if clash and confirm and not self._confirm(
                "覆盖确认", f"保存位置已存在 {len(clash)} 个同名文件（如 {clash[0]}），是否覆盖？"):
            return
        for e in files:
            self.manager.add_download(posixpath.join(self.cwd, e["name"]),
                                      os.path.join(dest, e["name"]), e["size"])
        for d in dirs:
            root = posixpath.join(self.cwd, d["name"])
            self.statusBar().showMessage(f"正在读取文件夹 {d['name']} 的内容…", 4000)
            run_in_background(
                lambda root=root: list_remote_tree(self.client, root),
                lambda items, d=d: self._enqueue_tree(items, dest, d["name"]),
                lambda e, d=d: self.statusBar().showMessage(f"读取文件夹 {d['name']} 失败：{e}", 8000))

    def _enqueue_tree(self, items, dest, dirname):
        for remote, rel, size in items:
            self.manager.add_download(remote, os.path.join(dest, dirname, *rel.split("/")), size)
        self.statusBar().showMessage(f"文件夹 {dirname}：已加入 {len(items)} 个下载任务", 5000)

    # ------------------------------------------------------------ 拖拽上传
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.upload_paths(paths)

    # ------------------------------------------------------------ 传输事件
    def _connected(self):
        return self._conn_state == "connected"

    def _on_task_added(self, task):
        self.transfers.add_task(task, self._connected())
        self._update_summary()

    def _on_task_updated(self, task):
        self.transfers.update_task(task, self._connected())
        self._update_summary()

    def _on_task_done(self, task):
        # 上传到当前目录的文件完成后自动刷新列表
        if task.direction == "upload" and posixpath.dirname(task.remote) == self.cwd:
            self._schedule_refresh()

    def _update_summary(self):
        ts = self.manager.tasks
        run = sum(1 for t in ts if t.state == RUNNING)
        wait = sum(1 for t in ts if t.state == WAITING)
        done = sum(1 for t in ts if t.state == DONE)
        self.summary_label.setText(f"传输中 {run}  排队 {wait}  已完成 {done}  ")

    # ------------------------------------------------------------ 关闭
    def closeEvent(self, event):
        if self.manager.active_count() and not self._confirm(
                "退出确认", "还有传输未完成，退出会暂停它们（下次可断点续传）。确定退出吗？"):
            event.ignore()
            return
        self.manager.shutdown()
        self.client.on_state = None
        self.client.close()
        event.accept()