import os
import numpy as np
from scanner import ScreenScanner, read_image_unicode

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_find_submit_buttons_and_exclude_self_window():
    scanner = ScreenScanner()
    targets = [
        {"name": "Submit (深色)", "path": "targets/submit_dark.png", "enabled": True},
        {"name": "Submit (淺色)", "path": "targets/submit_light.png", "enabled": True},
    ]
    scanner.load_templates(targets, BASE_DIR)

    canvas = np.zeros((600, 800, 3), dtype=np.uint8)
    dark_btn = read_image_unicode(os.path.join(BASE_DIR, "targets", "submit_dark.png"))
    light_btn = read_image_unicode(os.path.join(BASE_DIR, "targets", "submit_light.png"))

    dh, dw = dark_btn.shape[:2]
    lh, lw = light_btn.shape[:2]

    canvas[150:150 + dh, 100:100 + dw] = dark_btn
    canvas[320:320 + lh, 450:450 + lw] = light_btn

    matches = scanner.find_matches_in_image(
        canvas, offset_x=0, offset_y=0, threshold=0.85, multi_scale=False, exclude_rect=None
    )
    assert len(matches) == 2

    expected_dark_center = (100 + dw // 2, 150 + dh // 2)
    expected_light_center = (450 + lw // 2, 320 + lh // 2)

    centers = [(m.x, m.y) for m in matches]
    assert any(abs(cx - expected_dark_center[0]) <= 2 and abs(cy - expected_dark_center[1]) <= 2 for cx, cy in centers)
    assert any(abs(cx - expected_light_center[0]) <= 2 and abs(cy - expected_light_center[1]) <= 2 for cx, cy in centers)

    matches_excluded = scanner.find_matches_in_image(
        canvas, offset_x=0, offset_y=0, threshold=0.85, multi_scale=False, exclude_rect=(80, 130, 200, 100)
    )
    assert len(matches_excluded) == 1
    assert abs(matches_excluded[0].x - expected_light_center[0]) <= 2

