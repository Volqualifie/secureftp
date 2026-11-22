"""传输列表控件：每个任务一行（名称、大小、进度条、速度、状态、暂停/继续/移除按钮）。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QProgressBar, QStyle, QTableWidget,
    QTableWidgetItem, QToolButton, QWidget,
)

from .common import human_size, human_speed
from .transfers import DONE, FAILED, PAUSED, PAUSING, RUNNING, WAITING

COLS = ["名称", "大小", "进度", "速度", "状态", "操作"]


def status_text(task, connected=True) -> str:
    s = task.state
    if s == WAITING:
        return "排队中"
    if s == RUNNING:
        return "传输中" if connected else "网络中断，等待重连…"
    if s == PAUSING:
        return "正在暂停…"
    if s == PAUSED:
        return "已暂停（可继续）"
    if s == DONE:
        resumed = (task.result or {}).get("resumed_from", 0)
        return "完成（断点续传）" if resumed else "完成"
    return f"失败：{task.error}"


class _Buttons(QWidget):
    def __init__(self, style):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(2)
        self.toggle, self.remove = QToolButton(), QToolButton()
        self.icon_pause = style.standardIcon(QStyle.StandardPixmap.SP_MediaPause)
        self.icon_play = style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        self.remove.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogCloseButton))
        self.remove.setToolTip("移除")
        for b in (self.toggle, self.remove):
            b.setAutoRaise(True)
            lay.addWidget(b)


class TransferTable(QTableWidget):
    pause_requested = Signal(object)
    resume_requested = Signal(object)
    remove_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(0, len(COLS), parent)
        self._tasks = {}
        self.setHorizontalHeaderLabels(COLS)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        h = self.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, w in ((1, 90), (2, 190), (3, 90), (4, 190), (5, 80)):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.setColumnWidth(col, w)

    def _row(self, task):
        for r in range(self.rowCount()):
            if self.item(r, 0).data(Qt.ItemDataRole.UserRole) == task.id:
                return r
        return -1

    def add_task(self, task, connected=True):
        r = self.rowCount()
        self.insertRow(r)
        self._tasks[task.id] = task
        arrow = "↑" if task.direction == "upload" else "↓"
        name = QTableWidgetItem(f"{arrow} {task.name}")
        name.setData(Qt.ItemDataRole.UserRole, task.id)
        name.setToolTip(f"远程：/{task.remote}\n本地：{task.local}")
        self.setItem(r, 0, name)
        self.setItem(r, 1, QTableWidgetItem())
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setFormat("%p%")
        bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCellWidget(r, 2, bar)
        self.setItem(r, 3, QTableWidgetItem())
        self.setItem(r, 4, QTableWidgetItem())
        btns = _Buttons(self.style())
        btns.toggle.clicked.connect(lambda _=False, t=task, b=btns: self._on_toggle(t))
        btns.remove.clicked.connect(lambda _=False, t=task: self.remove_requested.emit(t))
        self.setCellWidget(r, 5, btns)
        self.update_task(task, connected)

    def _on_toggle(self, task):
        if task.state in (PAUSED, FAILED):
            self.resume_requested.emit(task)
        else:
            self.pause_requested.emit(task)

    def update_task(self, task, connected=True):
        r = self._row(task)
        if r < 0:
            return
        self.item(r, 1).setText(human_size(task.total) if task.total else "")
        bar = self.cellWidget(r, 2)
        if task.state == DONE:
            bar.setValue(1000)
        else:
            bar.setValue(0 if task.total <= 0 else min(1000, int(task.done * 1000 / task.total)))
        self.item(r, 3).setText(human_speed(task.speed) if task.state == RUNNING else "")
        text = status_text(task, connected)
        self.item(r, 4).setText(text)
        self.item(r, 4).setToolTip(text)
        btns = self.cellWidget(r, 5)
        if task.state in (PAUSED, FAILED):
            btns.toggle.setIcon(btns.icon_play)
            btns.toggle.setToolTip("继续（断点续传）")
            btns.toggle.setEnabled(True)
        else:
            btns.toggle.setIcon(btns.icon_pause)
            btns.toggle.setToolTip("暂停")
            btns.toggle.setEnabled(task.state in (WAITING, RUNNING))
        btns.toggle.setVisible(task.state != DONE)

    def remove_task(self, task):
        r = self._row(task)
        if r >= 0:
            self.removeRow(r)
        self._tasks.pop(task.id, None)

    def refresh_all(self, connected):
        for t in self._tasks.values():
            self.update_task(t, connected)