"""图形界面自动化测试（Qt 离屏模式，无需显示器）：真实服务端 + 真实 MainWindow。
覆盖：登录对话框、浏览、上传下载、文件夹上传下载、拖拽、暂停/继续、并发调度、
移除任务、失败重试、断网后状态指示与自动续传。
运行：python -m pytest tests/test_gui.py -v
可设置环境变量 GUI_SHOT_DIR=某目录 来保存界面截图。
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")      # 必须在导入 Qt 之前

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QPoint, QPointF, QSettings, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from client.ui.login_dialog import LoginDialog  # noqa: E402
from client.ui.main_window import MainWindow  # noqa: E402
from client.ui.transfers import DONE, FAILED, PAUSED, PAUSING, RUNNING, WAITING  # noqa: E402
from test_client import env, rnd  # noqa: E402,F401  (env 是 fixture)
from test_server_auth import PW, certs  # noqa: E402,F401


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait(cond, timeout=20.0):
    """一边处理 Qt 事件一边等待条件成立。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    QApplication.processEvents()
    return cond()


@pytest.fixture
def make_win(env, qapp):
    wins = []

    def make(srv=None, **kw):
        srv = srv or env.server()
        c = env.client(srv)
        kw.setdefault("history_path", str(env.tmp / "history.json"))     # 不要写到真实的用户目录
        win = MainWindow(c, "alice@test", **kw)
        win._confirm = lambda *a, **k: True            # 测试里不弹确认框
        wins.append(win)
        assert wait(lambda: win._nav_done >= 1)
        return win

    yield make
    for w in wins:
        w.close()
    QApplication.processEvents()


def all_done(win, n=None):
    ts = win.manager.tasks
    return (n is None or len(ts) == n) and ts and all(t.state == DONE for t in ts)


def read(path):
    with open(path, "rb") as f:
        return f.read()


def shot(widget, name, tmp_path):
    d = os.environ.get("GUI_SHOT_DIR") or str(tmp_path)
    os.makedirs(d, exist_ok=True)
    QApplication.processEvents()
    widget.grab().save(os.path.join(d, name))


# ------------------------------------------------------------- 登录对话框
def test_login_dialog_flow(env, qapp, tmp_path):
    srv = env.server()
    ini = str(tmp_path / "s.ini")

    def make():
        d = LoginDialog(settings=QSettings(ini, QSettings.Format.IniFormat))
        d.host.setText("127.0.0.1")
        d.port.setValue(srv.port)
        d.ca.setText(env.certs["ca-cert"])
        return d

    d = make()
    d.ok_btn.click()                                        # 什么都没填
    assert "请填写" in d.error.text()

    d.user.setText("alice")
    d.password.setText(PW)
    d.register_box.setChecked(True)
    d.ok_btn.click()
    assert wait(lambda: d.client is not None)
    env._clients.append(d.client)
    assert d.result() == d.DialogCode.Accepted and d.client.state == "connected"
    assert d.server_label == f"alice@127.0.0.1:{srv.port}"
    saved = QSettings(ini, QSettings.Format.IniFormat)
    assert saved.value("user") == "alice" and not saved.contains("password")   # 不保存密码

    d2 = make()                                             # 密码错误
    d2.user.setText("alice")
    d2.password.setText("WrongPass1")
    d2.ok_btn.click()
    assert wait(lambda: d2.error.text() != "" and d2.ok_btn.isEnabled())
    assert "用户名或密码错误" in d2.error.text() and d2.client is None

    d3 = make()                                             # CA 文件不存在
    d3.ca.setText(str(tmp_path / "nope.pem"))
    d3.user.setText("alice")
    d3.password.setText(PW)
    d3.ok_btn.click()
    assert wait(lambda: d3.ok_btn.isEnabled() and d3.error.text() != "")
    assert "找不到文件" in d3.error.text()

    d4 = make()                                             # 服务端证书不被信任
    d4.ca.setText(env.certs["client-cert"])
    d4.user.setText("alice")
    d4.password.setText(PW)
    d4.ok_btn.click()
    assert wait(lambda: d4.ok_btn.isEnabled() and d4.error.text() != "")
    assert "TLS" in d4.error.text() or "握手" in d4.error.text()
    shot(d4, "login_error.png", tmp_path)


# ------------------------------------------------------------- 浏览 / 上传 / 下载
def test_upload_browse_download(env, make_win, tmp_path):
    win = make_win()
    d1, d2 = rnd(300_000, 1), rnd(150_000, 2)
    p1, p2 = env.local("one.bin", d1), env.local("two.bin", d2)
    assert win.upload_paths([p1, p2]) == 2
    assert wait(lambda: all_done(win, 2))
    assert wait(lambda: win.files.rowCount() == 2)           # 上传完成后自动刷新
    assert {e["name"] for e in win._entries} == {"one.bin", "two.bin"}
    assert read(env.remote_path("one.bin")) == d1

    for t in win.manager.tasks:                              # 传输列表展示
        row = win.transfers._row(t)
        assert win.transfers.cellWidget(row, 2).value() == 1000
        assert win.transfers.item(row, 4).text() == "完成"

    win.files.selectAll()
    dest = str(tmp_path / "dest")
    os.makedirs(dest)
    win.download_entries(win._selected_entries(), dest)
    assert wait(lambda: len(win.manager.tasks) == 4 and all_done(win))
    assert read(os.path.join(dest, "one.bin")) == d1 and read(os.path.join(dest, "two.bin")) == d2
    assert "已完成 4" in win.summary_label.text()

    win.manager.clear_finished()
    assert win.manager.tasks == [] and win.transfers.rowCount() == 0


def test_folder_upload_navigate_and_folder_download(env, make_win, tmp_path):
    win = make_win()
    root = tmp_path / "proj"
    (root / "sub" / "deep").mkdir(parents=True)
    files = {"a.txt": b"alpha", "sub/b.bin": rnd(120_000, 3), "sub/deep/c.txt": "中文内容".encode()}
    for rel, data in files.items():
        (root / rel).write_bytes(data)
    assert win.upload_paths([str(root)]) == 3
    assert wait(lambda: all_done(win, 3))

    win.navigate("")
    assert wait(lambda: win._nav_done == win._nav_seq and [e["name"] for e in win._entries] == ["proj"])
    win._on_double_click(0, 0)                               # 双击目录进入
    assert wait(lambda: win.cwd == "proj" and win._nav_done == win._nav_seq)
    assert [e["name"] for e in win._entries] == ["sub", "a.txt"]   # 目录在前
    assert win.path_edit.text() == "/proj" and win.act_parent.isEnabled()
    win.go_parent()
    assert wait(lambda: win.cwd == "" and win._nav_done == win._nav_seq)
    assert not win.act_parent.isEnabled()

    dest = str(tmp_path / "out")
    os.makedirs(dest)
    before = len(win.manager.tasks)
    win.download_entries([e for e in win._entries if e["name"] == "proj"], dest)   # 下载整个文件夹
    assert wait(lambda: len(win.manager.tasks) == before + 3)
    assert wait(lambda: all(t.state == DONE for t in win.manager.tasks))
    for rel, data in files.items():
        assert read(os.path.join(dest, "proj", *rel.split("/"))) == data


def test_drag_and_drop_upload(env, make_win):
    win = make_win()
    p = env.local("dropped.txt", b"dragged in")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p)])
    enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    win.dragEnterEvent(enter)
    assert enter.isAccepted()
    win.dropEvent(QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                             Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert wait(lambda: all_done(win, 1))
    assert read(env.remote_path("dropped.txt")) == b"dragged in"

    text_only = QMimeData()
    text_only.setText("not a file")
    enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, text_only,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    enter.ignore()
    win.dragEnterEvent(enter)
    assert not enter.isAccepted()                            # 非文件拖入不接受


def test_overwrite_confirmation_can_cancel(env, make_win):
    win = make_win()
    p = env.local("same.txt", b"v1")
    win.upload_paths([p])
    assert wait(lambda: all_done(win, 1) and win.files.rowCount() == 1)
    asked = []
    win._confirm = lambda t, m: asked.append(t) or False     # 用户选择"否"
    assert win.upload_paths([p]) == 0 and asked == ["覆盖确认"]
    assert len(win.manager.tasks) == 1


# ------------------------------------------------------------- 暂停 / 继续 / 调度
def test_pause_then_resume_continues_from_breakpoint(env, make_win):
    win = make_win()
    data = rnd(80_000_000, 7)
    t = win.manager.add_upload(env.local("big.bin", data), "big.bin")
    paused_at = []

    def pause_when_partial(task):
        if task.state == RUNNING and task.done >= 5_000_000 and not paused_at:
            paused_at.append(task.done)
            win.manager.pause(task)

    win.manager.task_updated.connect(pause_when_partial)
    assert wait(lambda: t.state == PAUSED, 30)
    assert 0 < t.done < t.total                              # 真的停在了中间
    row = win.transfers._row(t)
    assert "已暂停" in win.transfers.item(row, 4).text()
    assert win.transfers.cellWidget(row, 5).toggle.toolTip().startswith("继续")
    win.manager.task_updated.disconnect(pause_when_partial)

    win.manager.resume(t)
    assert wait(lambda: t.state == DONE, 60)
    assert t.result["resumed_from"] > 0                      # 是续传，不是重头来
    assert "断点续传" in win.transfers.item(row, 4).text()
    assert read(env.remote_path("big.bin")) == data


def test_scheduler_respects_max_parallel(env, make_win):
    win = make_win(max_parallel=2)
    peak, seen_waiting = [0], [False]

    def watch(_task):
        ts = win.manager.tasks
        peak[0] = max(peak[0], sum(1 for x in ts if x.state in (RUNNING, PAUSING)))
        seen_waiting[0] |= any(x.state == WAITING for x in ts)

    win.manager.task_updated.connect(watch)
    datas = {f"f{i}.bin": rnd(3_000_000, 10 + i) for i in range(6)}
    for name, data in datas.items():
        win.manager.add_upload(env.local(name, data), name)
    assert wait(lambda: all_done(win, 6), 60)
    assert peak[0] <= 2 and seen_waiting[0]
    for name, data in datas.items():
        assert read(env.remote_path(name)) == data


def test_remove_running_and_waiting_tasks(env, make_win):
    win = make_win(max_parallel=1)
    big = win.manager.add_upload(env.local("big.bin", rnd(80_000_000, 5)), "big.bin")
    queued = win.manager.add_upload(env.local("q.bin", rnd(1000)), "q.bin")
    assert wait(lambda: big.state == RUNNING and big.done > 0)
    assert queued.state == WAITING

    win.manager.remove(queued)                               # 排队中的直接移除
    assert queued not in win.manager.tasks and win.transfers.rowCount() == 1
    win.manager.remove(big)                                  # 运行中的先停止再移除
    assert big.state == PAUSING
    assert wait(lambda: big not in win.manager.tasks and win.transfers.rowCount() == 0)


def test_failed_task_can_be_retried(env, make_win):
    win = make_win()
    path = env.tmp / "local" / "late.txt"
    t = win.manager.add_upload(str(path), "late.txt")        # 本地文件还不存在
    assert wait(lambda: t.state == FAILED)
    row = win.transfers._row(t)
    assert win.transfers.item(row, 4).text().startswith("失败")
    assert win.transfers.cellWidget(row, 5).toggle.isEnabled()      # 可点"继续"重试
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"now it exists")
    win.manager.resume(t)
    assert wait(lambda: t.state == DONE)
    assert read(env.remote_path("late.txt")) == b"now it exists"


# ------------------------------------------------------------- 断网
def test_connection_drop_updates_indicator_and_transfer_resumes(env, make_win):
    win = make_win()
    assert "已连接" in win.conn_label.text()
    states = []
    win.state_changed.connect(lambda s, d: states.append(s))
    data = rnd(80_000_000, 9)
    t = win.manager.add_upload(env.local("big.bin", data), "big.bin")
    dropped = []

    def drop_midway(task):
        if task.state == RUNNING and task.done >= 5_000_000 and not dropped:
            dropped.append(task.done)
            win.act_drop.trigger()                           # 点工具栏"模拟断网"

    win.manager.task_updated.connect(drop_midway)
    assert wait(lambda: t.state == DONE, 90)
    assert dropped and "reconnecting" in states and states[-1] == "connected"
    assert "已连接" in win.conn_label.text()
    assert read(env.remote_path("big.bin")) == data


def test_close_event_closes_client(env, make_win):
    win = make_win()
    c = win.client
    win.close()
    assert c.state == "closed"


# ------------------------------------------------------------- 截图（人工核对布局）
def test_screenshots(env, make_win, tmp_path):
    win = make_win(max_parallel=1)
    for name, n in (("report.pdf", 450_000), ("photo.jpg", 2_300_000)):
        win.upload_paths([env.local(name, rnd(n, n))])
        assert wait(lambda: all_done(win))
    sub = env.tmp / "local" / "notes"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "todo.txt").write_bytes(b"x")
    win.upload_paths([str(sub)])
    assert wait(lambda: all_done(win) and win.files.rowCount() == 3)

    big = win.manager.add_upload(env.local("movie.mkv", rnd(80_000_000, 1)), "movie.mkv")
    win.manager.task_updated.connect(
        lambda t: win.manager.pause(t) if t is big and t.state == RUNNING and t.done >= 8_000_000 else None)
    win.manager.add_upload(env.local("queued.zip", rnd(5_000_000, 2)), "queued.zip")
    win.manager.add_upload(str(env.tmp / "local" / "missing.txt"), "missing.txt")
    assert wait(lambda: big.state == PAUSED, 30)
    win.files.selectRow(0)
    win.resize(1040, 720)
    win.show()
    shot(win, "main_window.png", tmp_path)
    assert os.path.getsize(os.path.join(os.environ.get("GUI_SHOT_DIR") or str(tmp_path),
                                        "main_window.png")) > 5000


def test_become_vip_from_toolbar(env, make_win):
    srv = env.server(bandwidth_normal=600_000, bandwidth_vip=0)
    win = make_win(srv)
    assert "普通用户" in win.user_label.text() and win.act_vip.isEnabled()
    asked = []
    win._confirm = lambda t, m: asked.append(t) or False        # 先取消
    win.act_vip.trigger()
    assert asked == ["开通会员"] and not win.client.is_vip

    win._confirm = lambda *a: True
    win.act_vip.trigger()
    assert wait(lambda: win.client.is_vip)
    assert wait(lambda: "VIP 会员" in win.user_label.text())
    assert "不限速" in win.user_label.text()
    assert win.act_vip.text() == "已是会员" and not win.act_vip.isEnabled()


# ------------------------------------------------------------- 历史记录 / 删除
def test_history_survives_restart_and_can_resume(env, make_win):
    srv = env.server()
    win = make_win(srv)
    data = rnd(80_000_000, 21)
    small = rnd(50_000, 22)
    t = win.manager.add_upload(env.local("big.bin", data), "big.bin")
    win.manager.add_upload(env.local("small.bin", small), "small.bin")
    win.manager.task_updated.connect(
        lambda x: win.manager.pause(x) if x is t and x.state == RUNNING and x.done >= 5_000_000 else None)
    assert wait(lambda: t.state == PAUSED, 30)
    assert wait(lambda: any(x.name == "small.bin" and x.state == DONE for x in win.manager.tasks))
    hist = win.history_path
    win.close()                                              # 关闭客户端（模拟用户直接退出）
    assert os.path.exists(hist)

    win2 = make_win(srv, history_path=hist)                  # 重新打开 → 重新登录
    by_name = {x.name: x for x in win2.manager.tasks}
    assert set(by_name) == {"big.bin", "small.bin"}
    assert by_name["small.bin"].state == DONE
    big = by_name["big.bin"]
    assert big.state == PAUSED and 0 < big.done < big.total   # 没有自动开始，由用户选择
    row = win2.transfers._row(big)
    assert "已暂停" in win2.transfers.item(row, 4).text()
    assert win2.transfers.cellWidget(row, 2).value() > 0     # 进度条保留

    win2.manager.resume(big)                                 # 用户点"继续"
    assert wait(lambda: big.state == DONE, 60)
    assert big.result["resumed_from"] > 0                    # 确实是从断点继续
    assert read(env.remote_path("big.bin")) == data


def test_history_removed_item_and_corrupt_file(env, make_win, tmp_path):
    from client.ui.history import load_records
    srv = env.server()
    win = make_win(srv)
    win.manager.add_upload(env.local("a.txt", b"a"), "a.txt")
    assert wait(lambda: all_done(win, 1))
    win.manager.remove(win.manager.tasks[0])
    win.close()
    assert load_records(win.history_path) == []              # 移除后不再出现在历史里

    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert load_records(str(bad)) == []                      # 损坏的文件不会导致崩溃
    win3 = make_win(srv, history_path=str(bad))
    assert win3.manager.tasks == []


def test_delete_files_and_folders_from_gui(env, make_win):
    win = make_win()
    sub = env.tmp / "local" / "dir1"
    sub.mkdir(parents=True)
    (sub / "x.txt").write_bytes(b"x")
    win.upload_paths([env.local("keep.txt", b"k"), env.local("gone.txt", b"g"), str(sub)])
    assert wait(lambda: all_done(win, 3) and win.files.rowCount() == 3)

    win._confirm = lambda *a: False                          # 取消 → 不删
    win.files.selectAll()
    win.delete_selected()
    QApplication.processEvents()
    assert os.path.exists(env.remote_path("gone.txt"))

    win._confirm = lambda *a: True
    names = {e["name"]: i for i, e in enumerate(win._entries)}
    win.files.clearSelection()
    win.files.selectRow(names["gone.txt"])
    win.files.selectRow(names["gone.txt"])
    win.delete_entries([e for e in win._entries if e["name"] in ("gone.txt", "dir1")])
    assert wait(lambda: win.files.rowCount() == 1)
    assert [e["name"] for e in win._entries] == ["keep.txt"]
    assert not os.path.exists(env.remote_path("gone.txt"))
    assert not os.path.exists(env.remote_path("dir1"))
    assert os.path.exists(env.remote_path("keep.txt"))