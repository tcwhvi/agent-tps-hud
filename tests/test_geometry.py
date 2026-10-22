import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tps_overlay as m


class FakeWindow:
    """模拟 wm geometry 请求可能被丢弃的窗口：前 N 次请求不生效。"""

    def __init__(self, x, y, swallow=0):
        self.x = x
        self.y = y
        self.swallow = swallow
        self.requests = []

    def geometry(self, spec):
        self.requests.append(spec)
        if self.swallow > 0:
            self.swallow -= 1
            return
        match = re.search(r"([+-]\d+)([+-]\d+)$", spec)
        if match:
            self.x = int(match.group(1))
            self.y = int(match.group(2))

    def winfo_x(self):
        return self.x

    def winfo_y(self):
        return self.y


class GeometryConfirmTests(unittest.TestCase):
    def test_request_reports_target(self):
        win = FakeWindow(1915, 117)
        target = m.request_geometry(win, 190, 128, 1730, 117)
        self.assertEqual(target, (1730, 117))
        self.assertEqual(win.requests, ["190x128+1730+117"])

    def test_dropped_request_is_reissued_then_confirmed(self):
        win = FakeWindow(1915, 117, swallow=1)
        target = m.request_geometry(win, 190, 128, 1730, 117)
        self.assertEqual((win.x, win.y), (1915, 117))
        # 下一拍核对：没到位就重发，这次真的动了，但要再一拍才确认
        target = m.confirm_geometry(win, target, 190, 128)
        self.assertEqual((win.x, win.y), (1730, 117))
        self.assertIsNotNone(target)
        self.assertEqual(len(win.requests), 2)
        self.assertIsNone(m.confirm_geometry(win, target, 190, 128))
        self.assertEqual(len(win.requests), 2)

    def test_reissues_every_tick_until_the_window_actually_moves(self):
        win = FakeWindow(1915, 117, swallow=4)
        target = m.request_geometry(win, 190, 128, 1730, 117)
        rounds = 0
        while target is not None and rounds < 10:
            target = m.confirm_geometry(win, target, 190, 128)
            rounds += 1
        self.assertIsNone(target)
        self.assertEqual((win.x, win.y), (1730, 117))
        # 4 次被吞 + 1 次生效 + 1 次确认
        self.assertEqual(rounds, 5)
        self.assertEqual(len(win.requests), 5)

    def test_no_retry_once_landed(self):
        win = FakeWindow(1730, 117)
        target = m.confirm_geometry(win, (1730, 117), 190, 128)
        self.assertIsNone(target)
        self.assertEqual(win.requests, [])

    def test_no_target_means_nothing_pending(self):
        win = FakeWindow(1730, 117)
        self.assertIsNone(m.confirm_geometry(win, None, 190, 128))
        self.assertEqual(win.requests, [])


if __name__ == "__main__":
    unittest.main()
