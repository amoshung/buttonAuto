from region_selector import normalize_box


def test_normalize_box_handles_reverse_drag_and_min_size():
    box = normalize_box(300, 400, 100, 150, min_size=12)
    assert box == {"x": 100, "y": 150, "w": 200, "h": 250, "enabled": True}

    tiny = normalize_box(50, 50, 55, 58, min_size=12)
    assert tiny is None

