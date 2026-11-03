"""FunASR 已知 bug 的进程内补丁（可复用）。

funasr.utils.postprocess_utils.sentence_postprocess 在 token 与 timestamp
数组错位时抛 IndexError；补丁对齐长度后重试。仅修改当前进程，不修改项目与库文件。

用法（须在 import 使用方（如 monitor.jobs）之前调用）：
    from asr_compat import install_asr_patch
    install_asr_patch()
"""
from __future__ import annotations

_installed = False


def install_asr_patch() -> None:
    """给当前进程的 funasr 打 sentence_postprocess 补丁；幂等，重复调用无副作用。"""
    global _installed
    if _installed:
        return
    import funasr.utils.postprocess_utils as _ppu

    _orig_spp = _ppu.sentence_postprocess

    def _safe_sentence_postprocess(token, timestamp=None):
        try:
            return _orig_spp(token, timestamp)
        except IndexError:
            if timestamp is not None and token:
                if len(timestamp) < len(token):
                    pad = timestamp[-1] if timestamp else [0, 0]
                    ts = list(timestamp) + [pad] * (len(token) - len(timestamp))
                else:
                    ts = list(timestamp)[: len(token)]
                return _orig_spp(token, ts)
            raise

    _ppu.sentence_postprocess = _safe_sentence_postprocess
    _installed = True
