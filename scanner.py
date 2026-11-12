import os
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
import cv2
import numpy as np
import mss


@dataclass
class MatchResult:
    x: int
    y: int
    score: float
    target_name: str
    region_index: int = -1


def read_image_unicode(path: str) -> Optional[np.ndarray]:
    if not os.path.exists(path):
        return None
    try:
        raw = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


def write_image_unicode(path: str, img_bgr: np.ndarray) -> bool:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        ok, buf = cv2.imencode(".png", img_bgr)
        if not ok:
            return False
        buf.tofile(path)
        return True
    except Exception:
        return False


class ScreenScanner:
    def __init__(self) -> None:
        self._templates: List[Dict[str, Any]] = []

    def load_templates(self, targets: List[Dict[str, Any]], base_dir: str) -> None:
        loaded: List[Dict[str, Any]] = []
        for t in targets:
            if not t.get("enabled", True):
                continue
            rel_or_abs = t.get("path", "")
            full_path = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(base_dir, rel_or_abs)
            img = read_image_unicode(full_path)
            if img is not None and img.size > 0:
                loaded.append({
                    "name": t.get("name", os.path.basename(full_path)),
                    "path": rel_or_abs,
                    "img": img,
                    "h": img.shape[0],
                    "w": img.shape[1],
                })
        self._templates = loaded

    def find_matches_in_image(
        self,
        img_bgr: np.ndarray,
        offset_x: int = 0,
        offset_y: int = 0,
        threshold: float = 0.85,
        multi_scale: bool = False,
        exclude_rect: Optional[Tuple[int, int, int, int]] = None,
        region_index: int = -1,
    ) -> List[MatchResult]:
        results: List[MatchResult] = []
        ih, iw = img_bgr.shape[:2]
        scales = [0.9, 0.95, 1.0, 1.05, 1.1] if multi_scale else [1.0]

        for tmpl in self._templates:
            base_img = tmpl["img"]
            best_score = -1.0
            best_center: Optional[Tuple[int, int]] = None

            for scale in scales:
                if abs(scale - 1.0) < 1e-4:
                    t_img = base_img
                else:
                    nw = max(8, int(tmpl["w"] * scale))
                    nh = max(8, int(tmpl["h"] * scale))
                    t_img = cv2.resize(base_img, (nw, nh), interpolation=cv2.INTER_AREA)

                th, tw = t_img.shape[:2]
                if ih < th or iw < tw:
                    continue

                res = cv2.matchTemplate(img_bgr, t_img, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= threshold and max_val > best_score:
                    cx = offset_x + max_loc[0] + tw // 2
                    cy = offset_y + max_loc[1] + th // 2
                    if exclude_rect is not None:
                        ex, ey, ew, eh = exclude_rect
                        if ex <= cx <= (ex + ew) and ey <= cy <= (ey + eh):
                            continue
                    best_score = float(max_val)
                    best_center = (cx, cy)

            if best_center is not None:
                if not any(abs(r.x - best_center[0]) < 20 and abs(r.y - best_center[1]) < 20 for r in results):
                    results.append(
                        MatchResult(
                            x=best_center[0],
                            y=best_center[1],
                            score=best_score,
                            target_name=tmpl["name"],
                            region_index=region_index,
                        )
                    )

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def scan_screen(
        self,
        regions: List[Dict[str, Any]],
        threshold: float = 0.85,
        multi_scale: bool = False,
        exclude_rect: Optional[Tuple[int, int, int, int]] = None,
    ) -> List[MatchResult]:
        if not self._templates:
            return []

        active_regions = [r for r in regions if r.get("enabled", True) and r.get("w", 0) > 10 and r.get("h", 0) > 10]
        all_matches: List[MatchResult] = []

        with mss.mss() as sct:
            if not active_regions:
                mon = sct.monitors[0]
                raw = np.array(sct.grab(mon))
                img_bgr = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
                return self.find_matches_in_image(
                    img_bgr,
                    offset_x=mon["left"],
                    offset_y=mon["top"],
                    threshold=threshold,
                    multi_scale=multi_scale,
                    exclude_rect=exclude_rect,
                    region_index=-1,
                )

            for idx, reg in enumerate(active_regions):
                bbox = {
                    "left": int(reg["x"]),
                    "top": int(reg["y"]),
                    "width": int(reg["w"]),
                    "height": int(reg["h"]),
                }
                try:
                    raw = np.array(sct.grab(bbox))
                    img_bgr = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
                    matches = self.find_matches_in_image(
                        img_bgr,
                        offset_x=bbox["left"],
                        offset_y=bbox["top"],
                        threshold=threshold,
                        multi_scale=multi_scale,
                        exclude_rect=exclude_rect,
                        region_index=idx,
                    )
                    for m in matches:
                        if not any(abs(existing.x - m.x) < 20 and abs(existing.y - m.y) < 20 for existing in all_matches):
                            all_matches.append(m)
                except Exception:
                    continue

        all_matches.sort(key=lambda r: r.score, reverse=True)
        return all_matches

