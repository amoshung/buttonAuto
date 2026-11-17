"""子程序環境變數覆蓋值（可能含金鑰）的遮蔽與合併。"""
import os
from typing import Dict, Mapping, Optional


class SecretEnv(dict):
    """環境變數覆蓋值；repr／str 只顯示鍵名，避免金鑰出現在日誌、例外或 repr。

    值為 None 表示「從子程序環境移除這個變數」（見 merge_env）。
    """

    def __repr__(self) -> str:
        return "SecretEnv(%s)" % ", ".join(sorted(self.keys()))

    __str__ = __repr__

    def copy(self) -> "SecretEnv":
        return SecretEnv(self)


def merge_env(overlay: Optional[Mapping[str, Optional[str]]]) -> Dict[str, str]:
    """os.environ 合併 overlay；overlay 值為 None 的鍵會被移除。"""
    env = dict(os.environ)
    for k, v in (overlay or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env
