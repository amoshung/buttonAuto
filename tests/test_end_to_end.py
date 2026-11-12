import os
import numpy as np
from config import AppConfig
from scanner import ScreenScanner, read_image_unicode

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_end_to_end_pipeline_with_default_config_and_targets(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg = AppConfig.load(str(cfg_path))
    scanner = ScreenScanner()
    scanner.load_templates(cfg.targets, BASE_DIR)

    assert len(scanner._templates) == 2

    screen = np.full((500, 700, 3), 240, dtype=np.uint8)
    light_btn = read_image_unicode(os.path.join(BASE_DIR, "targets", "submit_light.png"))
    lh, lw = light_btn.shape[:2]
    screen[220:220 + lh, 310:310 + lw] = light_btn

    matches = scanner.find_matches_in_image(
        screen, offset_x=0, offset_y=0, threshold=cfg.threshold, multi_scale=cfg.multi_scale
    )
    assert len(matches) == 1
    assert matches[0].x == 310 + lw // 2
    assert matches[0].y == 220 + lh // 2

